"""Operator procedures; command execution and claim-aware notes are injected by B17.

Only a host operator with the cutover claim can call these procedures. Remote
transfer uses the existing archiver Transport, and removal remains exclusively
in the existing host reap script. Exception text and command output are never
copied to cutover notes.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, Protocol

from axcommon import schemas, timeutil
from core.archiver.guard import Guard
from core.archiver.manifest import build_state, same_stat, stat_state

ENABLE_SOURCES = frozenset({"mp-runs-archive", "ci-public-logs", "gh-auto-wt", "codex-runs"})
STOP_REASONS = frozenset({"emergency", "inconsistency"})


class CommandResult(Protocol):
    returncode: int
    stdout: str


@dataclass(frozen=True)
class Ports:
    """B17 adapters. authorize renews cutover and fails closed on invalid owner.

    execute takes argv without a shell. note wraps the shared claim-aware record
    writer, returning its exit status. restart_archiver uses the common executor
    and the caller's existing compose/profile configuration. No production
    implementation or fallback is selected here.
    """
    authorize: Callable[[], bool]
    execute: Callable[[list[str]], CommandResult]
    note: Callable[[str, str, str, str, str], int]
    restart_archiver: Callable[[], bool]


@dataclass(frozen=True)
class Paths:
    state_dir: Path
    sources_file: Path
    cutover_archive: Path
    changelog: Path
    host_id: str

    def __post_init__(self):
        for path in (self.state_dir, self.sources_file, self.cutover_archive, self.changelog):
            if not path.is_absolute():
                raise ValueError("absolute_profile_path_required")
        if not self.host_id or any(c in self.host_id for c in "/\r\n"):
            raise ValueError("invalid_host_id")
        # Never snapshot into the verified tree itself.
        archive = self.cutover_archive.resolve()
        state = self.state_dir.resolve()
        if archive == state or state in archive.parents or archive in state.parents:
            raise ValueError("overlapping_archive_and_state")

    @property
    def marker(self) -> Path:
        return self.state_dir / "archive" / "reap-enabled"

    @property
    def verified(self) -> Path:
        return self.state_dir / "archive" / "verified"

    @property
    def journal(self) -> Path:
        return self.state_dir / "cutover" / "cutover-state.jsonl"

    @property
    def activation(self) -> Path:
        return self.state_dir / "archive" / "reap-activation.json"


@dataclass(frozen=True)
class Outcome:
    result: str
    reason: str
    archive: Path | None = None
    recorded: bool = False


class ReapOperator:
    def __init__(self, paths: Paths, ports: Ports):
        self.paths, self.ports = paths, ports

    def _authorized(self) -> bool:
        try:
            return self.ports.authorize() is True
        except Exception:
            return False

    def _note(self, subject: str, result: str, reason: str) -> bool:
        try:
            target = f"{subject if subject == 'T-78' else 'reap'}/{self.paths.host_id}"
            return self.ports.note("cutover.state", subject, target, result, reason) == 0
        except Exception:
            return False

    def _old_disabled(self) -> bool:
        for unit in ("archive-to-hdd.service", "archive-to-hdd.timer"):
            active = self.ports.execute(["systemctl", "--user", "is-active", unit])
            enabled = self.ports.execute(["systemctl", "--user", "is-enabled", unit])
            if (active.returncode != 3 or active.stdout.strip() != "inactive"
                    or enabled.returncode != 1 or enabled.stdout.strip() not in {"disabled", "masked"}):
                return False
        return True

    def _t78_ok(self) -> bool:
        """The latest local cutover.state record, never a caller-supplied boolean."""
        from tools.cutover.common import latest_result, read_records

        records = [event for event in read_records(self.paths.journal)
                   if event["source"].get("host") == self.paths.host_id]
        return latest_result(records, "T-78") == "ok"

    @staticmethod
    def _plain_tree(root: Path) -> None:
        """Do not follow links while copying a manifest snapshot."""
        if root.is_symlink() or not root.is_dir():
            raise ValueError("verified_directory_required")
        for item in root.rglob("*"):
            mode = item.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError("non_regular_manifest_tree")

    def _sources_and_manifests(self):
        import yaml

        if self.paths.sources_file.is_symlink():
            raise ValueError("sources_symlink")
        doc = yaml.safe_load(self.paths.sources_file.read_text(encoding="utf-8"))
        sources = doc.get("sources") if isinstance(doc, dict) else doc
        if not isinstance(sources, list) or not sources:
            raise ValueError("sources_required")
        seen = set()
        self._plain_tree(self.paths.verified)
        for source in sources:
            name = source["name"]
            if not isinstance(name, str) or name in seen or not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
                raise ValueError("invalid_source_name")
            seen.add(name)
            manifests = list((self.paths.verified / name).glob("*.json"))
            valid = False
            for manifest in manifests:
                if manifest.name.endswith(".reap.json"):
                    continue
                content = json.loads(manifest.read_text(encoding="utf-8"))
                schemas.validate("ext/b1/archive-manifest", content)
                if content["name"] != name:
                    raise ValueError("manifest_source_mismatch")
                valid |= bool(content["files"])
            if not valid:
                raise ValueError("verified_manifest_missing")
        return doc, sources

    def _archive_dir(self, prefix: str) -> Path:
        stamp = timeutil.now().strftime("%Y%m%dT%H%M%S%fZ")
        self.paths.cutover_archive.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory = self.paths.cutover_archive / f"{prefix}-{stamp}"
        directory.mkdir(mode=0o700)
        return directory

    @staticmethod
    def _write_new(path: Path, value: dict) -> None:
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def enable(self, *, t2_ok: bool) -> Outcome:
        """F-15 stage 6. A missing prerequisite leaves the marker absent."""
        if not self._authorized():
            return Outcome("waiting", "cutover_claim_required")
        if self.paths.activation.exists():
            return Outcome("skipped", "already_activated_use_resume")
        if os.path.lexists(self.paths.marker):
            return Outcome("failed", "unexpected_existing_marker")
        try:
            if not self._old_disabled():
                return Outcome("waiting", "old_archiver_not_stopped_and_disabled")
            if t2_ok is not True or not self._t78_ok():
                return Outcome("waiting", "verification_not_passed")
            doc, sources = self._sources_and_manifests()
        except Exception:
            return Outcome("waiting", "prerequisite_unavailable")
        archive = None
        try:
            archive = self._archive_dir("reap-enable")
            shutil.copy2(self.paths.sources_file, archive / "archive-sources.yaml")
            import yaml

            for source in sources:
                if source["name"] in ENABLE_SOURCES:
                    source["remove_original"] = True
                elif source["name"] == "claude-projects":
                    source["remove_original"] = False
            staged = archive / "archive-sources.next.yaml"
            staged.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
            os.chmod(staged, stat.S_IMODE(self.paths.sources_file.stat().st_mode))
            # Retain the staged document and the original; only the installed copy changes.
            with self.paths.sources_file.open("w", encoding="utf-8") as stream:
                stream.write(staged.read_text(encoding="utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if self.ports.restart_archiver() is not True:
                return Outcome("failed", "archiver_restart_failed_copy_only", archive)
            # Recheck immediately before opening the removal gate.
            if not self._authorized() or not self._old_disabled():
                return Outcome("waiting", "enable_gate_changed_copy_only", archive)
            targets = sorted(s["name"] for s in sources if s["name"] in ENABLE_SOURCES)
            stamp = timeutil.now_ts()
            self._write_new(self.paths.activation, {"at": stamp, "sources": targets, "archive": str(archive)})
            fd = os.open(self.paths.marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.fsync(fd)
            os.close(fd)
            self.paths.changelog.parent.mkdir(parents=True, exist_ok=True)
            with self.paths.changelog.open("a", encoding="utf-8") as stream:
                stream.write(f"\n- {stamp}: reap enabled: {', '.join(targets)}; claude-projects remains copy-only.\n")
                stream.flush()
                os.fsync(stream.fileno())
            return Outcome("ok", "enabled", archive)
        except Exception:
            reason = "enable_incomplete_marker_present" if self.paths.marker.exists() else "enable_incomplete_copy_only"
            return Outcome("failed", reason, archive)

    def stop(self, *, reason: str) -> Outcome:
        if reason not in STOP_REASONS:
            return Outcome("skipped", "not_an_emergency_or_inconsistency")
        if not self._authorized():
            return Outcome("waiting", "cutover_claim_required")
        if not os.path.lexists(self.paths.marker):
            return Outcome("skipped", "already_stopped")
        archive = None
        try:
            if self.paths.marker.is_symlink() or not self.paths.marker.is_file() or self.paths.marker.stat().st_size:
                raise ValueError("invalid_marker")
            self._plain_tree(self.paths.verified)
            archive = self._archive_dir("reap-stop")
            shutil.copytree(self.paths.verified, archive / "verified", copy_function=shutil.copy2)
            self._write_new(archive / "stop.json", {"reason": reason, "at": timeutil.now_ts()})
            if not self._authorized():
                return Outcome("waiting", "cutover_claim_required", archive)
            self.paths.marker.rename(archive / "reap-enabled")
        except Exception:
            return Outcome("failed", "stop_incomplete", archive)
        recorded = self._note("reap-stop", "ok", reason)
        return Outcome("ok" if recorded else "failed", "stopped" if recorded else "stopped_note_failed", archive, recorded)

    def resume(self, archive: Path) -> Outcome:
        if not self._authorized():
            return Outcome("waiting", "cutover_claim_required")
        try:
            if (archive.is_symlink() or archive.parent.resolve() != self.paths.cutover_archive.resolve()
                    or not archive.name.startswith("reap-stop-")):
                raise ValueError("invalid_stop_archive")
            saved = archive / "reap-enabled"
            if saved.is_symlink() or not saved.is_file() or saved.stat().st_size:
                raise ValueError("saved_marker_required")
            if os.path.lexists(self.paths.marker):
                return Outcome("failed", "marker_already_present", archive)
            if not (archive / "stop.json").is_file():
                raise ValueError("stop_record_required")
            # The existing host script performs size/mtime_ns/inode rechecks on
            # each current manifest entry during its next cycle. Do not reap here.
            self._plain_tree(self.paths.verified)
            saved.rename(self.paths.marker)
        except Exception:
            return Outcome("failed", "resume_incomplete", archive)
        recorded = self._note("reap-resume", "ok", "manifest_stat_recheck_next_cycle")
        return Outcome("ok" if recorded else "failed", "resumed" if recorded else "resumed_note_failed", archive, recorded)

    def verify_t78(self, transport, *, relative_path: str, guard: Guard | None = None) -> Outcome:
        """Copy and checksum-verify one old claude-projects file; never remove it.

        B17 passes its existing Transport with profile archive_dest and restricted
        key. Tests pass a local fake Transport. No network implementation is built
        or selected by this package.
        """
        if not self._authorized():
            return Outcome("waiting", "cutover_claim_required")
        result, reason = "failed", "archive_verification_failed"
        try:
            import yaml

            doc = yaml.safe_load(self.paths.sources_file.read_text(encoding="utf-8"))
            sources = doc.get("sources") if isinstance(doc, dict) else doc
            source = next(s for s in sources if s["name"] == "claude-projects")
            if source.get("remove_original", False) or self.paths.marker.exists():
                raise ValueError("t78_requires_copy_only")
            root = Path(source["path"]).expanduser()
            guard = guard or Guard.from_environment()
            guard.check_source_root(root)
            rel = Path(relative_path)
            target = root / rel
            if rel.is_absolute() or ".." in rel.parts or not guard.allowed(root, target):
                raise ValueError("invalid_probe_file")
            before = build_state(root, relative_path, source.get("keep_original", ()))
            oldest = (timeutil.now() - timedelta(days=max(7, source.get("min_age_days", 7)))).timestamp()
            if before is None or before.mtime_ns / 1e9 > oldest:
                raise ValueError("old_probe_file_required")
            dest = f"claude-projects/{timeutil.now_ts()[:10]}"
            transport.ensure_dirs(dest)
            transport.send(root, [relative_path], dest)
            transport.verify(root, [relative_path], dest)
            if not same_stat(before, stat_state(root, relative_path)):
                raise ValueError("probe_changed_after_verification")
            result, reason = "ok", "content_checksum_verified_copy_only"
        except Exception:
            pass
        recorded = self._note("T-78", result, reason)
        return Outcome(result if recorded else "failed", reason if recorded else "t78_note_failed", recorded=recorded)

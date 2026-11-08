"""Operator-only cutover primitives; no daemon, no implicit execution (S-24)."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Mapping, Protocol, Sequence

from axcommon.ids import is_ulid, new_ulid
from axcommon.profile import _load_yaml, build_env

ROOT = Path(__file__).resolve().parents[2]
RESULTS = frozenset({"ok", "failed", "waiting", "skipped"})


class CutoverError(RuntimeError):
    """A local operator step cannot proceed. Never starts old services."""


class GateBlocked(CutoverError):
    pass


class IntegrationPending(NotImplementedError):
    """A package adapter has not been connected yet."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    planned: bool = False


class Runner(Protocol):
    def run(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
            timeout: int = 30) -> CommandResult: ...


@dataclass
class CommandRunner:
    """Default runner records a plan. apply is an explicit operator choice."""
    dry_run: bool = True
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, argv, *, env=None, timeout=30):
        argv = tuple(str(x) for x in argv)
        self.calls.append(argv)
        if self.dry_run:
            return CommandResult(0, planned=True)
        try:
            p = subprocess.run(argv, env={**os.environ, **(env or {})},
                               capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            # Output can contain credentials. Do not include it in exceptions or notes.
            raise CutoverError("command_unavailable") from exc
        return CommandResult(p.returncode, p.stdout, p.stderr)


@dataclass(frozen=True)
class Profile:
    """Host values come from the existing profile, never this package."""
    host: str
    ax_state_dir: Path
    tmp_dir: Path
    values: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not self.host or not self.ax_state_dir.is_absolute() or not self.tmp_dir.is_absolute():
            raise ValueError("invalid_profile")

    @classmethod
    def load(cls, path: Path) -> "Profile":
        raw = _load_yaml(path)
        env = build_env(raw)
        return cls(env["AX_HOST"], Path(env["AX_STATE_DIR"]), Path(env["AX_TMP_DIR"]), raw)

    @property
    def record_path(self) -> Path:
        return self.ax_state_dir / "cutover" / "cutover-state.jsonl"


@dataclass(frozen=True)
class Authority:
    go_file: Path
    approval_id: str
    build_go: Path | None = None

    def verify(self, runner: Runner, changes: Sequence[int] = ()) -> dict:
        # Existence is only an input check; authority comes exclusively from intake.
        if not self.go_file.is_file():
            raise GateBlocked("go_missing")
        if not is_ulid(self.approval_id):
            raise GateBlocked("approval_id_invalid")
        argv = ["ax", "approval", "verify", self.approval_id, "--go-file", str(self.go_file)]
        if self.build_go is not None:
            argv += ["--build-go", str(self.build_go)]
        argv += ["--json"]
        result = runner.run(argv)
        if result.planned:
            raise GateBlocked("approval_not_verified")
        try:
            response = json.loads(result.stdout)
            d = response["data"]
            checks = d["checks"]
            valid = (result.returncode == 0 and response["ok"] is True
                     and d["approval_id"] == self.approval_id and d["kind"] == "removal"
                     and d["state"] == "granted" and d["executed"] is True
                     and d["scope"] in ("removal", "both")
                     and checks["record"] == "ok" and checks["go_file"] == "ok"
                     and (self.build_go is None or checks["build_go"] == "ok"))
            approved = d["changes"]
            valid = valid and isinstance(approved, list) and all(n in approved for n in changes)
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise GateBlocked("approval_verify_failed")
        return d


def read_records(path: Path) -> list[dict]:
    """Read the operator log, rejecting incomplete or untrusted-shaped lines."""
    if not path.exists():
        return []
    records = []
    for line in path.read_bytes().splitlines():
        try:
            event = json.loads(line)
            source = event["source"]
            valid = (len(line) + 1 < 4000 and event["kind"] == "cutover.state"
                     and isinstance(event["subject"], str) and bool(event["subject"])
                     and isinstance(event["ts"], str)
                     and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", event["ts"])
                     and event["project"] is None and event["subloop"] is None
                     and is_ulid(event["id"]) and is_ulid(event["cause"])
                     and source["component"] == "cutover-operator"
                     and re.fullmatch(r"[a-z0-9][a-z0-9-]{2,31}", source["principal"])
                     and isinstance(source["session"], str) and 0 < len(source["session"]) <= 64
                     and source["session"].isprintable()
                     and source["via"] in ("local", "ssh", "tailnet", "hq")
                     and event["data"]["result"] in RESULTS)
        except (ValueError, KeyError, TypeError):
            continue
        if valid:
            records.append(event)
    return records


def latest_result(records: Sequence[dict], subject: str) -> str | None:
    matches = [e for e in records if e["subject"] == subject]
    # Millisecond timestamps can tie. The later appended line wins that tie.
    return max(enumerate(matches), key=lambda pair: (pair[1]["ts"], pair[0]))[1]["data"]["result"] if matches else None


@dataclass
class Context:
    profile: Profile
    archive_dir: Path
    runner: Runner = field(default_factory=CommandRunner)
    dry_run: bool = True
    authority: Authority | None = None
    notes: list[dict] = field(default_factory=list)

    def guard(self, changes: Sequence[int] = ()) -> None:
        if self.dry_run:
            return
        if self.authority is None:
            raise GateBlocked("go_missing")
        self.authority.verify(self.runner, changes)
        r = self.runner.run(["ax", "claim", "renew", "cutover", "--json", "--request-id", new_ulid()])
        try:
            response = json.loads(r.stdout)
            d = response["data"]
            owner = d["owner"]
            valid = (r.returncode == 0 and not r.planned and response["ok"] is True
                     and d["holder"] is True and is_ulid(d["claim_event_id"])
                     and re.fullmatch(r"[a-z0-9][a-z0-9-]{2,31}", owner["principal"])
                     and isinstance(owner["session"], str) and 0 < len(owner["session"]) <= 64
                     and owner["session"].isprintable() and owner["via"] in ("local", "ssh", "tailnet", "hq"))
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise GateBlocked("cutover_claim_missing")

    def run(self, argv: Sequence[str], *, env=None, timeout=30, changes: Sequence[int] = ()) -> CommandResult:
        if self.dry_run:
            return CommandResult(0, planned=True)
        self.guard(changes)
        return self.runner.run(argv, env=env, timeout=timeout)

    def note(self, subject: str, target: str, result: str, reason: str = "", *, at: str | None = None,
             kind: str = "cutover.state") -> str | None:
        if result not in RESULTS or kind not in ("cutover.state", "removal.state", "removal.check"):
            raise ValueError("invalid_note")
        if reason and not re.fullmatch(r"[a-z0-9_.:/-]{1,120}", reason):
            raise ValueError("reason_must_be_code")
        if at is not None and subject != "cutover-time":
            raise ValueError("timestamp_subject_invalid")
        if self.dry_run:
            self.notes.append({"kind": kind, "subject": subject, "target": target, "result": result, "planned": True})
            return None
        self.guard()
        env = {"AX_CUTOVER_DIR": str(self.profile.record_path.parent), "CUTOVER_AT": at or ""}
        r = self.runner.run([sys.executable, "-I", str(ROOT / "tools" / "cutover_note.py"),
                             kind, subject, target, result, reason], env=env)
        if r.returncode != 0 or r.planned or not is_ulid(r.stdout.strip()):
            raise CutoverError("cutover_note_failed")
        self.notes.append({"kind": kind, "subject": subject, "target": target, "result": result})
        return r.stdout.strip()

    def records(self) -> list[dict]:
        return read_records(self.profile.record_path)

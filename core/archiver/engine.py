"""退避の 1 巡（S-14.3、F-20）。

退避元ごとに: 候補の列挙 → ハッシュ → 退避先へ rsync --checksum → --dry-run の再実行で差分 0 件 → manifest → 元の扱い。
元を除去する動作:
  - ホストのパスの退避元（kind=host。ro の bind）: archiver は消さない。remove_original が true の退避元だけ、照合に成功した manifest を
    ${ax_state_dir}/archive/verified/<name>/<日付>.json に書く。除去はホスト側の host/ax-archive-reap.sh（印 reap-enabled がある間だけ）。
  - AX のボリュームの archive/（kind=volume）: 照合の後、印 reap-enabled がある間だけ archiver 自身が移す。印が無い間はコピーだけ。
    除去の直前に「大きさ・mtime・inode が manifest と同じ」ことを再確認し、違えば残す。keep の印のファイルは除去しない。
失敗（接続不可、鍵の拒否、照合の不一致、空き不足）: 元を残し、その退避元だけを保留にして archive.failed を書く。同じ原因は 1 件のカード。他の退避元は続ける。
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from axcommon import timeutil
from axcommon.redact import Redactor, _RE_EMAIL

from . import manifest as mf
from .config import Source
from .guard import Guard, SourceRefused
from .ledger import ArchiverLedger
from .rsync import TransferError, Transport

log = logging.getLogger("archiver")

REAP_MARKER = "reap-enabled"
DEFAULT_MAX_FILES = 2000


class Emitter(Protocol):
    def emit(self, kind: str, subject: str, data: dict | None = None, **kw): ...


class Notifier(Protocol):
    def failure(self, source: str, reason: str, detail: str, *, first: bool, event_id: str | None) -> str | None: ...


@dataclass
class SourceReport:
    name: str
    candidates: int = 0
    transferred: int = 0
    bytes: int = 0
    removed: int = 0
    kept: int = 0
    changed_after: int = 0
    skipped_known: int = 0
    failed: str | None = None
    refused: bool = False


@dataclass
class RunReport:
    date: str
    sources: list[SourceReport] = field(default_factory=list)


class Archiver:
    def __init__(self, sources: list[Source], transport: Transport, ledger: ArchiverLedger, emitter: Emitter, *, state_dir: str | Path,
                 guard: Guard | None = None, notifier: Notifier | None = None, max_files: int = DEFAULT_MAX_FILES,
                 now: Callable[[], object] | None = None):
        self.sources, self.transport, self.ledger, self.emitter = sources, transport, ledger, emitter
        self.state_dir = Path(state_dir)
        self.guard = guard or Guard()
        self.notifier = notifier
        self.max_files = max_files
        self.stage_root = Path(os.environ.get("TMPDIR", "")) / "ax-archiver"

    # --- 印 -------------------------------------------------------------------------------------
    @property
    def verified_dir(self) -> Path:
        return self.state_dir / "archive" / "verified"

    def reap_enabled(self) -> bool:
        return (self.state_dir / "archive" / REAP_MARKER).exists()

    # --- 列挙 -----------------------------------------------------------------------------------
    def candidates(self, src: Source) -> list[str]:
        """min_age_days より古い通常のファイルの相対パス（古い順）。リンクと秘密置き場は除く。"""
        cutoff_ns = timeutil.now().timestamp() * 1e9 - src.min_age_days * 86400 * 1e9
        found: list[tuple[int, str]] = []
        root = src.root
        if self._is_stage(root):
            return []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink() and d not in self.guard.names and not self._is_stage(Path(dirpath) / d))
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                rel = p.relative_to(root).as_posix()
                if src.include and not mf.matches_any(rel, src.include):
                    continue
                if src.exclude and mf.matches_any(rel, src.exclude):
                    continue
                if not self.guard.allowed(root, p):
                    continue
                st = mf.stat_state(root, rel)
                if st is None or st.mtime_ns >= cutoff_ns:
                    continue
                if self.ledger.known(src.name, rel, st.size, st.mtime_ns):
                    continue
                found.append((st.mtime_ns, rel))
        found.sort()
        return [r for _, r in found]

    def _is_stage(self, path: Path) -> bool:
        try:
            return path.samefile(self.stage_root)
        except OSError:
            return False

    # --- 1 巡 -----------------------------------------------------------------------------------
    def run_once(self) -> RunReport:
        date = timeutil.now_ts()[:10]
        report = RunReport(date)
        for src in self.sources:
            rep = SourceReport(src.name)
            report.sources.append(rep)
            try:
                self._run_source(src, date, rep)
            except SourceRefused as e:
                rep.refused = True
                rep.failed = "secrets_dir_refused"
                self._fail(src, "secrets_dir_refused", str(e), rep)
            except TransferError as e:
                rep.failed = e.reason
                self._fail(src, e.reason, e.detail, rep)
            except Exception as e:  # noqa: BLE001  1 つの退避元の想定外の失敗で、他の退避元を止めない
                log.exception("退避元 %s で想定外の失敗", src.name)
                rep.failed = "internal_error"
                self._fail(src, "internal_error", type(e).__name__, rep)
        return report

    def _run_source(self, src: Source, date: str, rep: SourceReport) -> None:
        if not src.root.is_dir():
            return  # 存在しない退避元は飛ばす（F-10: 無い物は登録しない）
        self.guard.check_source_root(src.root)
        rels = self.candidates(src)
        rep.candidates = len(rels)
        if not rels:
            self.ledger.clear_failures(src.name)
            return
        rels = rels[: self.max_files]
        index_path = os.environ.get("AX_REDACT_INDEX")
        pii_path = os.environ.get("AX_PII_LIST")
        if not index_path or not pii_path or not os.environ.get("TMPDIR"):
            raise TransferError("inspection_unavailable", "inspection configuration unavailable")
        try:
            scanner = Redactor(index_path=index_path, pii_list_path=pii_path, allow_email_domains=[])
            if not scanner.index_ok or not scanner.pii_list_ok:
                raise ValueError("inspection configuration unavailable")
            self.stage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.stage_root.is_symlink() or self.stage_root.stat().st_mode & 0o077:
                raise ValueError("unsafe staging directory")
            stage = Path(tempfile.mkdtemp(prefix="archive-", dir=self.stage_root))
        except (OSError, ValueError, UnicodeError):
            raise TransferError("inspection_unavailable", "inspection configuration or staging unavailable") from None
        states: list[mf.FileState] = []
        held = False
        for rel in rels:
            try:
                if scanner.contains_secret(src.name + "/" + rel) or _RE_EMAIL.search(src.name + "/" + rel):
                    held = True
                    continue
                before = mf.stat_state(src.root, rel)
                if before is None or not self.guard.allowed(src.root, src.root / rel):
                    held = True
                    continue
                target = stage / rel
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(src.root / rel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as source, open(target, "xb") as copied:
                    shutil.copyfileobj(source, copied)
                os.chmod(target, 0o600)
                os.utime(target, ns=(before.mtime_ns, before.mtime_ns))
                if not mf.same_stat(before, mf.stat_state(src.root, rel)):
                    held = True
                    continue
                text = target.read_text(encoding="utf-8")
                if scanner.contains_secret(text) or _RE_EMAIL.search(text):
                    held = True
                    continue
                states.append(mf.FileState(rel, before.size, before.mtime_ns, before.inode,
                                           mf.sha256_file(target), mf.matches_any(rel, src.keep_original)))
            except (OSError, UnicodeError, ValueError):
                held = True
                continue
        if held:
            rep.failed = "inspection_held"
            self._fail(src, "inspection_held", "one or more candidates could not be approved", rep)
        if not states:
            return
        dest_rel = f"{src.name}/{date}"
        self.transport.ensure_dirs(dest_rel)
        paths = [s.path for s in states]
        self.transport.send(stage, paths, dest_rel)
        self.transport.verify(stage, paths, dest_rel)
        # 照合の後で変わったファイルは、この巡回では「照合済み」にしない（元を残す）
        ok: list[mf.FileState] = []
        for s in states:
            if mf.same_stat(s, mf.stat_state(src.root, s.path)):
                ok.append(s)
            else:
                rep.changed_after += 1
        if not ok:
            return
        local_manifest = self._manifest_path(src, date)
        doc = mf.write_manifest(local_manifest, src.name, date, ok)
        try:
            manifest_dir = Path(tempfile.mkdtemp(prefix="manifest-", dir=self.stage_root))
            manifest_copy = manifest_dir / local_manifest.name
            with local_manifest.open("rb") as source, manifest_copy.open("xb") as copied:
                shutil.copyfileobj(source, copied)
            manifest_text = manifest_copy.read_text(encoding="utf-8")
            if scanner.contains_secret(manifest_text) or _RE_EMAIL.search(manifest_text):
                raise ValueError("manifest inspection failed")
        except (OSError, UnicodeError, ValueError):
            raise TransferError("inspection_held", "manifest could not be approved") from None
        self.transport.put_file(manifest_copy, dest_rel)
        self.ledger.record(src.name, ok, dest_rel, timeutil.now_ts())
        rep.transferred = len(ok)
        rep.bytes = sum(s.size for s in ok)
        rep.kept = sum(1 for s in ok if s.keep)
        if src.kind == "volume" and src.remove_original and self.reap_enabled():
            rep.removed = self._remove_volume_originals(src, ok)
        if not held:
            self.ledger.clear_failures(src.name)
        self.emitter.emit("archive.moved", src.name, {"from": src.name, "to": dest_rel, "bytes": rep.bytes, "files": rep.transferred,
                                                     "removed": rep.removed, "kept": rep.kept})
        del doc

    def _manifest_path(self, src: Source, date: str) -> Path:
        """host の退避元で remove_original が true のときだけ verified/ に置く（reap が読む）。それ以外は作業用の場所（送るための写し）。"""
        if src.kind == "host" and src.remove_original:
            return self.verified_dir / src.name / f"{date}.json"
        return self.state_dir / "archive" / "manifests" / src.name / f"{date}.json"

    def _remove_volume_originals(self, src: Source, states: list[mf.FileState]) -> int:
        removed = 0
        for s in states:
            if s.keep:
                continue
            cur = mf.stat_state(src.root, s.path)
            if not mf.same_stat(s, cur):
                continue  # 照合の後で書き換えられた物は残す
            if not self.guard.allowed(src.root, src.root / s.path):
                continue
            try:
                os.unlink(src.root / s.path)  # 照合済みの物を移す（退避の中の移動の一部。削除の API は外に出さない）
                removed += 1
            except OSError:
                continue
        return removed

    # --- 失敗 -----------------------------------------------------------------------------------
    def _fail(self, src: Source, reason: str, detail: str, rep: SourceReport) -> None:
        now = timeutil.now_ts()
        info = self.ledger.note_failure(src.name, reason, now)
        ev = None
        if info["first"] or info["count"] % 24 == 0:  # 同じ原因は最初の 1 回と、24 巡（約 1 日）ごとだけ事象にする
            ev = self.emitter.emit("archive.failed", src.name, {"source": src.name, "reason": reason, "detail": detail[:200], "count": info["count"]})
        if info["first"] and self.notifier is not None:
            try:
                state = self.notifier.failure(src.name, reason, detail, first=True, event_id=(ev or {}).get("id") if isinstance(ev, dict) else None)
                self.ledger.mark_notified(src.name, reason, now, state or "unknown")
            except Exception:  # noqa: BLE001
                log.exception("カードを作れない")

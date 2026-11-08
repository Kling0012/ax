"""manifest（F-20）: 相対パス、大きさ、mtime、inode、SHA-256、keep。

ファイル 1 つの状態を FileState で持つ。除去の前の再確認は「大きさ・mtime（ナノ秒）・inode が manifest と同じ」（F-20、S-14.3）。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from axcommon import timeutil


@dataclass(frozen=True)
class FileState:
    path: str       # 退避元の root からの相対パス（/ 区切り）
    size: int
    mtime_ns: int
    inode: int
    sha256: str = ""
    keep: bool = False

    def to_json(self) -> dict:
        d = {"path": self.path, "size": self.size, "mtime_ns": self.mtime_ns, "inode": self.inode, "sha256": self.sha256}
        if self.keep:
            d["keep"] = True
        return d


def stat_state(root: Path, rel: str) -> FileState | None:
    """リンクをたどらずに stat する。通常のファイルでなければ None。"""
    try:
        st = os.lstat(root / rel)
    except OSError:
        return None
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        return None
    return FileState(rel, st.st_size, st.st_mtime_ns, st.st_ino)


def same_stat(a: FileState, b: FileState | None) -> bool:
    return b is not None and (a.size, a.mtime_ns, a.inode) == (b.size, b.mtime_ns, b.inode)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def matches_any(rel: str, patterns) -> bool:
    """glob（fnmatch。* は / も越える）。`**` は `*` と同じに扱う。先頭の `*/` が無い形も許す。"""
    for pat in patterns:
        p = pat.replace("**", "*")
        if fnmatch.fnmatchcase(rel, p):
            return True
        if p.startswith("*/") and fnmatch.fnmatchcase(rel, p[2:]):
            return True
    return False


def build_state(root: Path, rel: str, keep_patterns=()) -> FileState | None:
    """stat → ハッシュ → もう一度 stat。途中で変わったファイルは None（次の巡回で見直す）。"""
    before = stat_state(root, rel)
    if before is None:
        return None
    try:
        digest = sha256_file(root / rel)
    except OSError:
        return None
    after = stat_state(root, rel)
    if not same_stat(before, after):
        return None
    return FileState(rel, before.size, before.mtime_ns, before.inode, digest, matches_any(rel, keep_patterns))


def write_manifest(path: Path, name: str, date: str, states, *, merge: bool = True) -> dict:
    """manifest を一時ファイルから置き換えで書く。同じ日の既存の manifest があれば、パスごとに新しい方で重ねる。"""
    files: dict[str, dict] = {}
    if merge and path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            files = {f["path"]: f for f in old.get("files", [])}
        except (OSError, ValueError, KeyError, TypeError):
            files = {}
    for s in states:
        files[s.path] = s.to_json()
    doc = {"version": 1, "name": name, "date": date, "created_at": timeutil.now_ts(), "files": [files[k] for k in sorted(files)]}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return doc

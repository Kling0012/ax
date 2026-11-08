"""Small host-side filesystem operations, executed only through an injected runner.

The program uses stdin JSON and stdout JSON. No subprocess, network, deletion,
copy/unlink fallback, or service operations are available here.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import sys


def measure(path: Path, seen: set[tuple[int, int]] | None = None) -> tuple[int, int]:
    """Match find -type f count and du -sb bytes, without following links."""
    if seen is None:
        seen = set()
    st = path.lstat()
    count = int(stat.S_ISREG(st.st_mode))
    inode = (st.st_dev, st.st_ino)
    size = 0 if inode in seen else st.st_size
    seen.add(inode)
    if stat.S_ISDIR(st.st_mode):
        for child in path.iterdir():
            n, b = measure(child, seen)
            count += n
            size += b
    return count, size


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _private_dir(path: Path) -> None:
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    owner = parent.stat()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.geteuid() == 0:
        for created in missing:
            os.chown(created, owner.st_uid, owner.st_gid)
    path.chmod(0o700)


def _append_once(path: Path, line: str) -> None:
    _private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.geteuid() == 0:
        owner = path.parent.stat()
        os.fchown(fd, owner.st_uid, owner.st_gid)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        lines = stream.read().splitlines()
        if line and line not in lines:
            stream.seek(0, os.SEEK_END)
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _archive_name() -> str:
    return os.environ.get("AX_RETIREMENT_ARCHIVE_NAME", "issue-automation-old")


def _rows(archive: Path) -> list[list[str]]:
    manifest = archive / _archive_name() / "manifest.tsv"
    if not manifest.exists():
        return []
    return [line.split("\t") for line in manifest.read_text().splitlines() if line]


def _validate_moves(archive: Path, moves: list[list[str]]) -> None:
    for source, target in moves:
        src, dst = Path(source), Path(target)
        if not _exists(src):
            continue
        if _exists(dst):
            raise FileExistsError("destination-exists")
        parent = dst.parent
        while not parent.exists():
            parent = parent.parent
        if src.lstat().st_dev != parent.stat().st_dev:
            raise OSError("cross-filesystem")
        # The archive itself must also be on the source filesystem except for
        # explicit compute-host same-disk destinations supplied by the caller.
        if any(c in str(src) + str(dst) for c in "\t\r\n"):
            raise ValueError("invalid-manifest-path")


def _move(archive: Path, host: str, moves: list[list[str]], at: str) -> list[str]:
    _private_dir(archive)
    _validate_moves(archive, moves)  # Check the entire batch before its first move.
    moved = []
    for source, target in moves:
        src, dst = Path(source), Path(target)
        old = next((r for r in _rows(archive) if r[0:3] == [host, source, target]), None)
        if not _exists(src):
            if old and (not _exists(dst) or list(map(int, old[3:5])) != list(measure(dst))):
                raise ValueError("archived-content-mismatch")
            continue
        _private_dir(dst.parent)
        before = measure(src)
        # Intent is durable before rename so interruption never loses origin.
        intent = json.dumps({"host": host, "source": source, "target": target,
                             "files": before[0], "bytes": before[1], "at": at}, sort_keys=True)
        _append_once(archive / _archive_name() / "move-intents.jsonl", intent)
        os.rename(src, dst)  # EXDEV fails; never use shutil.move's copy fallback.
        if _exists(src) or measure(dst) != before:
            raise ValueError("move-content-mismatch")
        row = "\t".join([host, source, target, str(before[0]), str(before[1])])
        _append_once(archive / _archive_name() / "manifest.tsv", row)
        _append_once(archive / "README.md", f"- {at} {host}: `{source}` -> `{target}`; restore with mv to the original path.")
        moved.append(source)
    return moved


def _recover(archive: Path) -> None:
    path = archive / _archive_name() / "move-intents.jsonl"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        row = json.loads(line)
        src, dst = Path(row["source"]), Path(row["target"])
        if not _exists(src) and _exists(dst):
            if measure(dst) != (row["files"], row["bytes"]):
                raise ValueError("archive-intent-mismatch")
            manifest = "\t".join([row["host"], str(src), str(dst), str(row["files"]), str(row["bytes"])])
            _append_once(archive / _archive_name() / "manifest.tsv", manifest)
            _append_once(archive / "README.md", f"- {row['at']} {row['host']}: `{src}` -> `{dst}`; restore with mv to the original path.")


def handle(data: dict) -> dict:
    op = data["op"]
    if op == "busy":
        held = []
        for pattern in data.get("lock_patterns", []):
            import glob
            for name in glob.glob(pattern):
                with open(name, "rb") as stream:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        held.append(name)
        jobs = Path(data["jobs"]) if data.get("jobs") else None
        if jobs and jobs.exists() and any(jobs.iterdir()):
            held.append("jobs")
        return {"busy": bool(held), "items": ["lock-or-jobs"] if held else []}
    archive = Path(data["archive"])
    if op == "prepare":
        _private_dir(archive)
        # F-5.1: protect the AX and archive parent directories as well.
        _private_dir(archive.parent)
        _private_dir(archive.parent.parent)
        _append_once(archive / _archive_name() / "manifest.tsv", "")
        _recover(archive)
        _append_once(archive / "README.md", "Old components: no deletion. nft tables, Docker images and users remain; kernel nft tables vanish on reboot. Worktree .git links recover when moved to the original paths. Restore only on user instruction, with AX external operations in collect_only first; set old config enabled=false and dry-run before enabling timers. compute-host storage must precede its prune units on restore.")
        return {}
    if op == "glob":
        import glob
        return {"paths": sorted(set(name for pattern in data["patterns"] for name in glob.glob(pattern)))}
    if op == "preflight":
        _validate_moves(archive, data["moves"])
        return {}
    if op == "move":
        return {"moved": _move(archive, data["host"], data["moves"], data["at"])}
    if op == "save":
        path = Path(data["path"])
        _private_dir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(data["text"])
        return {}
    if op == "fstab":
        path = Path(data["path"])
        backup = archive / _archive_name() / f"etc-fstab.{data['host']}"
        text = path.read_text()
        if not backup.exists():
            _private_dir(backup.parent)
            backup.write_text(text)
            backup.chmod(0o600)
        changed = "".join("# ax-cutover: " + line if "ghauto-sbx/sod-gamedata" in line and not line.lstrip().startswith("#") else line for line in text.splitlines(keepends=True))
        if changed != text:
            path.write_text(changed)
        return {}
    if op == "verify":
        _recover(archive)
        rows = _rows(archive)
        for host, source, target, count, size in rows:
            if _exists(Path(source)) or not _exists(Path(target)):
                raise ValueError("manifest-location-mismatch")
            if measure(Path(target)) != (int(count), int(size)):
                raise ValueError("manifest-content-mismatch")
        for name in data.get("required", []):
            if not Path(name).exists():
                raise ValueError("compute-host-replacement-missing")
        return {"components": [r[1] for r in rows]}
    raise ValueError("unknown-file-operation")


if __name__ == "__main__":
    try:
        print(json.dumps(handle(json.load(sys.stdin))))
    except Exception as exc:
        # Do not echo paths, command output, or potentially secret contents.
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        sys.exit(1)

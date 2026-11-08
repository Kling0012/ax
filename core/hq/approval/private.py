"""HQ-private factor state. No intake, seats, gate or host configuration writes."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import threading


class PrivateStore:
    def __init__(self, private_dir: str | Path):
        self.directory = Path(private_dir)
        self._thread_lock = threading.RLock()

    def _directories(self):
        # The host creates hq/. Do not repair permissions or create arbitrary
        # ancestors when a deployment supplies a wrong path.
        parent = self.directory.parent
        self._check(parent, 0o700, directory=True)
        if not self.directory.exists():
            self.directory.mkdir(mode=0o700)
        self._check(self.directory, 0o700, directory=True)

    @staticmethod
    def _check(path: Path, mode: int, *, directory=False):
        st = path.lstat()
        kind = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
        if not kind or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != mode:
            raise PermissionError("hq_private_permissions")
        if not directory and st.st_nlink != 1:
            raise PermissionError("hq_private_hardlink")

    @contextmanager
    def locked(self):
        with self._thread_lock:
            self._directories()
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
            fd = os.open(self.directory / "approval-factor.lock", flags, 0o600)
            try:
                self._check(self.directory / "approval-factor.lock", 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield self
            finally:
                os.close(fd)

    def read(self, name="approval-totp") -> dict:
        path = self.directory / name
        try:
            self._check(path, 0o600)
        except FileNotFoundError:
            return {}
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("hq_private_state")
        return value

    def write(self, value: dict, name="approval-totp"):
        path = self.directory / name
        if path.exists() or path.is_symlink():
            self._check(path, 0o600)
        # Atomic replacement keeps replay/lock state intact across a crash. All
        # temporary state remains in this same private HDD-backed bind mount.
        fd, tmp = tempfile.mkstemp(prefix=".factor-", dir=self.directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dfd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

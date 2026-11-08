"""Write-only HQ slots (S-22.7, F-13.1, F-21.1)."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import stat
import uuid


class SecretFailure(Exception):
    def __init__(self, code: str, status: int = 422):
        self.code, self.status = code, status
        super().__init__(code)


NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}\Z")


class SecretSlots:
    def __init__(self, directory: str | Path, definitions: dict, *, registered=None, emit=None, intake=None):
        self.directory = Path(directory)
        self.definitions = definitions
        self.registered = registered or (lambda env, name: False)
        self.emit, self.intake = emit, intake

    def _definition(self, slot, operation="write"):
        if not isinstance(slot, str) or not NAME.fullmatch(slot):
            raise SecretFailure("slot_unknown")
        definition = self.definitions.get(slot)
        if not isinstance(definition, dict):
            raise SecretFailure("slot_unknown")
        filename = definition.get("file", slot)
        if not isinstance(filename, str) or not NAME.fullmatch(filename):
            raise SecretFailure("slot_invalid")
        if operation not in definition.get("operations", []):
            raise SecretFailure("operation_not_allowed", 403)
        target = definition.get("target")
        if target == {"local": True}:
            return definition, filename, False
        if (not isinstance(target, dict) or set(target) != {"env", "name"} or filename != slot
                or not all(isinstance(v, str) and v for v in target.values())
                or not self.registered(target["env"], target["name"])):
            raise SecretFailure("target_unregistered", 409)
        return definition, filename, True

    def metadata(self):
        rows = []
        for slot in sorted(self.definitions):
            try:
                _, filename, _ = self._definition(slot)
                path = self.directory / filename
                info = path.lstat() if path.exists() else None
                configured = bool(info and stat.S_ISREG(info.st_mode) and info.st_size > 0)
                rows.append({"name": slot, "configured": configured, "registered": True})
            except (SecretFailure, OSError):
                rows.append({"name": slot, "configured": False, "registered": False})
        return rows

    def _validate(self, definition, value):
        if not isinstance(value, str) or not value:
            raise SecretFailure("value_invalid")
        maximum = definition.get("max_length")
        pattern = definition.get("pattern")
        if (isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 65536
                or not isinstance(pattern, str) or len(pattern) > 1024):
            raise SecretFailure("slot_invalid")
        raw = value.encode("utf-8")
        if len(raw) > maximum or "\x00" in value:
            raise SecretFailure("value_invalid")
        try:
            valid = re.fullmatch(pattern, value)
        except re.error:
            raise SecretFailure("slot_invalid") from None
        if not valid:
            raise SecretFailure("value_invalid")
        return raw

    def _write(self, filename, raw):
        root = self.directory
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or root.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o770:
            raise SecretFailure("slot_directory_unsafe", 503)
        # All directory entries are flat and opened without following symlinks.
        # Group readers do not receive directory write permission via the socket.
        dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            lockfd = os.open(".write-lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
            with os.fdopen(lockfd, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    oldfd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
                except FileNotFoundError:
                    oldfd = None
                except OSError as exc:
                    raise SecretFailure("slot_file_unsafe", 503) from exc
                if oldfd is not None:
                    with os.fdopen(oldfd, "rb") as old:
                        oldinfo = os.fstat(old.fileno())
                        if not stat.S_ISREG(oldinfo.st_mode) or oldinfo.st_nlink != 1:
                            raise SecretFailure("slot_file_unsafe", 503)
                        try:
                            os.mkdir("archive", 0o700, dir_fd=dirfd)
                        except FileExistsError:
                            pass
                        try:
                            arcfd = os.open("archive", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dirfd)
                        except OSError as exc:
                            raise SecretFailure("slot_archive_unsafe", 503) from exc
                        try:
                            arcinfo = os.fstat(arcfd)
                            if not stat.S_ISDIR(arcinfo.st_mode) or arcinfo.st_uid != os.getuid() or stat.S_IMODE(arcinfo.st_mode) != 0o700:
                                raise SecretFailure("slot_archive_unsafe", 503)
                            saved = os.open(filename + "." + uuid.uuid4().hex, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640, dir_fd=arcfd)
                            with os.fdopen(saved, "wb") as out:
                                os.fchmod(out.fileno(), 0o640)
                                while chunk := old.read(65536):
                                    out.write(chunk)
                                out.flush()
                                os.fsync(out.fileno())
                        finally:
                            os.close(arcfd)
                staging = ".incoming-" + uuid.uuid4().hex
                fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o640, dir_fd=dirfd)
                with os.fdopen(fd, "wb") as out:
                    os.fchmod(out.fileno(), 0o640)
                    out.write(raw)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(staging, filename, src_dir_fd=dirfd, dst_dir_fd=dirfd)
                os.fsync(dirfd)
        finally:
            os.close(dirfd)

    def store(self, slot, value, *, operation="write", request_id=None):
        definition, filename, remote = self._definition(slot, operation)
        raw = self._validate(definition, value)
        if self.emit is None:
            raise SecretFailure("event_unavailable", 503)
        if remote and (self.intake is None or not isinstance(request_id, str) or not NAME.fullmatch(request_id)):
            raise SecretFailure("push_request_required")
        try:
            self._write(filename, raw)
            self.emit("secret.stored", slot, {"name": slot})
        except SecretFailure:
            raise
        except Exception:
            raise SecretFailure("secret_store_failed", 503) from None
        if remote:
            try:
                status, _ = self.intake.submit("secret.push", {"slot": slot}, idempotency_key="secret.push:" + request_id)
            except Exception:
                raise SecretFailure("push_unavailable", 503) from None
            if not 200 <= status < 300:
                raise SecretFailure("push_rejected", 503)
        return {"stored": True, "slot": slot, "push_requested": remote}

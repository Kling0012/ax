"""Instabox driver using an injected ibox executable and its existing credentials."""
from __future__ import annotations

import base64
import binascii
import shlex
import subprocess
import os
import signal
import math
import json
from pathlib import Path
import time
from pathlib import PurePosixPath
from typing import Mapping, Any


class InstaboxError(RuntimeError):
    pass


class InstaboxTimeout(InstaboxError):
    def __init__(self, message, *, logs=()):
        super().__init__(message)
        self.logs = tuple(logs)


class InstaboxCancelled(InstaboxError):
    pass


class UnsupportedOperation(InstaboxError):
    pass


class InstaboxDriver:
    def __init__(self, executable: str, *, runner=None, timeout: float = 600, cancel_grace: float = 2):
        if not isinstance(executable, str) or not executable:
            raise ValueError("ibox executable is required")
        if (not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
                or not isinstance(cancel_grace, (int, float)) or not math.isfinite(cancel_grace) or cancel_grace < 0):
            raise ValueError("invalid process time limit")
        self.executable = executable
        self._runner = runner or subprocess.Popen
        self.timeout = timeout
        self.cancel_grace = cancel_grace

    def _call(self, *args: str, cancel_event=None, timeout=None) -> bytes:
        if cancel_event is not None and cancel_event.is_set():
            raise InstaboxCancelled("ibox operation cancelled")
        try:
            process = self._runner([self.executable, *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        except Exception:
            raise InstaboxError("ibox operation failed") from None

        def signal_group(sig):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass

        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        try:
            while True:
                cancelled = cancel_event is not None and cancel_event.is_set()
                expired = time.monotonic() >= deadline
                if cancelled or expired:
                    signal_group(signal.SIGTERM)
                    try:
                        process.communicate(timeout=self.cancel_grace)
                    except subprocess.TimeoutExpired:
                        signal_group(signal.SIGKILL)
                        process.communicate()
                    if cancelled:
                        raise InstaboxCancelled("ibox operation cancelled")
                    raise InstaboxTimeout("ibox operation timed out")
                try:
                    stdout, _ = process.communicate(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
                    if process.returncode != 0:
                        raise InstaboxError("ibox operation failed")
                    return stdout
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                signal_group(signal.SIGKILL)
                process.communicate()
            raise

    def create(self):
        raise UnsupportedOperation("machine creation is unsupported")

    def destroy(self, machine_id=None):
        raise UnsupportedOperation("machine deletion is unsupported")

    def status(self):
        return self._call("status").decode("utf-8", "replace")

    def execute(self, request: Mapping[str, Any], *, cancel_event=None):
        from core.resources.envs.adapters.cloud_sandbox import _scan
        if not isinstance(request, Mapping) or request.get("sensitivity") != "public" or request.get("secrets"):
            raise InstaboxError("public input without secrets is required")
        _scan(request)
        entry = request.get("entry")
        command = entry.get("command") if isinstance(entry, Mapping) else None
        if isinstance(command, str):
            try:
                command = shlex.split(command)
            except ValueError:
                raise InstaboxError("invalid command") from None
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise InstaboxError("command is required")
        workdir = request.get("workdir", "/data/workspace")
        artifacts = request.get("artifacts", [])
        spec = {"job_id": request["job_id"], "workdir": workdir, "command": command,
                "timeout_seconds": self.timeout, "artifacts": artifacts}
        script = Path(__file__).with_name("ax_sbx_run.py")
        remote = "/data/workspace/.ax/ax-sbx-run.py"
        # The existing ibox file transfer is the only deployment operation.
        _scan({"sensitivity": "public", "content": script.read_text(encoding="utf-8")})
        self._call("put", str(script), remote, cancel_event=cancel_event)
        cmd = "python3 " + shlex.quote(remote) + " " + shlex.quote(json.dumps(spec, ensure_ascii=True))
        output = self._call("exec", cmd, cancel_event=cancel_event,
                            timeout=self.timeout + self.cancel_grace + 5)
        try:
            result = json.loads(output)
            if (not isinstance(result, dict) or type(result.get("exit_code")) is not int
                    or type(result.get("timed_out")) is not bool
                    or not isinstance(result.get("stdout"), str) or not isinstance(result.get("stderr"), str)
                    or not isinstance(result.get("artifacts"), list)):
                raise ValueError
            names = []
            for item in result["artifacts"]:
                if not isinstance(item, dict) or item.get("path") not in artifacts:
                    raise ValueError
                path = PurePosixPath(workdir) / item["path"]
                if not path.is_absolute() or ".." in path.parts:
                    raise ValueError
                names.append(str(path))
        except (ValueError, TypeError, KeyError):
            raise InstaboxError("invalid sandbox response") from None
        if result["timed_out"]:
            raise InstaboxTimeout("sandbox operation timed out",
                                  logs=(x for x in (result["stdout"], result["stderr"]) if x))
        return {"ok": result["exit_code"] == 0 and not result["timed_out"],
                "logs": [x for x in (result["stdout"], result["stderr"]) if x], "artifacts": names,
                "artifact_hashes": dict(zip(names, (item.get("sha256") for item in result["artifacts"]))) }

    def fetch(self, name: str, *, cancel_event=None) -> bytes:
        if not isinstance(name, str) or not name or '\x00' in name:
            raise InstaboxError("invalid artifact path")
        path = PurePosixPath(name)
        if not path.is_absolute() or '..' in path.parts:
            raise InstaboxError("invalid artifact path")
        output = self._call("exec", "base64 -w0 -- " + shlex.quote(name), cancel_event=cancel_event)
        try:
            return base64.b64decode(output, validate=True)
        except (binascii.Error, ValueError):
            raise InstaboxError("invalid artifact response") from None

    def put(self, content: bytes, remote_path: str, *, sensitivity: str, scan_outbound=None):
        """Send explicitly public UTF-8 content after the shared outbound scan."""
        from core.resources.envs.adapters.cloud_sandbox import _scan
        if sensitivity != "public" or not isinstance(content, bytes):
            raise InstaboxError("public content required")
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            raise InstaboxError("content cannot be scanned") from None
        if not isinstance(remote_path, str) or not remote_path or "\x00" in remote_path:
            raise InstaboxError("invalid remote path")
        path = PurePosixPath(remote_path)
        if not path.is_absolute() or ".." in path.parts:
            raise InstaboxError("invalid remote path")
        request = {"sensitivity": sensitivity, "content": text, "path": remote_path}
        _scan(request)
        if scan_outbound is not None:
            result = scan_outbound(request.copy())
            if result is False or (isinstance(result, (list, tuple, set)) and result):
                raise InstaboxError("outbound content rejected")
        encoded = base64.b64encode(content).decode("ascii")
        command = "mkdir -p -- " + shlex.quote(str(path.parent)) + " && printf %s " + shlex.quote(encoded) + " | base64 -d > " + shlex.quote(remote_path)
        self._call("exec", command)

"""Injectable finite Streamable HTTP fake. It never binds or connects a socket.

The optional shell executor uses only a caller-supplied HDD temporary directory.
Synthetic wire fields are fixtures, not claims about an unmeasured provider API.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import shlex
import struct
import subprocess
import tempfile
import zlib
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from core.resources.box.box_mcp import ALLOWED_TOOLS, RUNNER_VERSION


def png(width=1280, height=800):
    def chunk(name, body):
        return struct.pack("!I", len(body)) + name + body + struct.pack("!I", zlib.crc32(name + body))
    header = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = zlib.compress((b"\0" + b"\xff\xff\xff" * width) * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", data) + chunk(b"IEND", b"")


@dataclass
class RequestRecord:
    name: str
    args: dict
    args_bytes: int


class FakeClock:
    def __init__(self):
        self.now = 1800000000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    advance = sleep


class BoxFake:
    """Callable transport matching tests/fakes/http.py's injection convention.

    inject(name, status|Exception|result) queues one fault. Set revoked_keys for
    independent path authentication. Request records contain no auth headers.
    """

    def __init__(self, *, state="hibernated", sse=False, root=None, clock=None, execute_shell=False):
        self.clock = clock or FakeClock()
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="box-fake-", dir=os.environ["TMPDIR"]))
        self.root.mkdir(parents=True, exist_ok=True)
        self.execute_shell = execute_shell
        self.state, self.sse = state, sse
        self.machines = [{"name": "main", "id": "fake-box-1"}]
        self.tools = set(ALLOWED_TOOLS)
        self.cost_today = self.cost_month = 0
        self.billing = {"isError": True, "content": [{"type": "text", "text": "box plans are not enabled"}]}
        self.image, self.desktop_image = png(), png()
        self.windows = []
        self.elements = [{"ref": "fake-ref-1", "role": "link"}]
        self.runner_version = RUNNER_VERSION
        self.checks = {key: "ok" for key in ("sh", "chromium", "node", "playwright", "fullpage", "docker")}
        self.disk_free_mib = 4096
        self.eigen_cli, self.eigen_id = False, False
        self.flock, self.setsid = True, True
        self.write_limit = None
        self.disk_full = False
        self.truncate_at = 40000
        self.truncate_fetch_above = None
        self.busy = False
        self.wake_delay = 0
        self.wake_started = None
        self.child_survives_reset = True
        self.reset_on_shell = False
        self.sessions = set()
        self.session_counter = 0
        self.protocol_versions = []
        self.expire_next = False
        self.revoked_keys = set()
        self.calls = []
        self.faults = defaultdict(deque)
        self.jobs = {}
        self.artifacts = {}
        self.initialized_notifications = 0
        self.snapshots_count = 14
        self.install_failures = 0

    def inject(self, name, fault):
        self.faults[name].append(fault)
        return self

    def count(self, name):
        return sum(call.name == name for call in self.calls)

    def names(self):
        return [call.name for call in self.calls]

    def clear_calls(self):
        # Clear only in-memory observations; no files are removed.
        self.calls.clear()

    def _reply(self, request_id, result, *, status=200, session=None):
        headers = {"Content-Type": "text/event-stream" if self.sse else "application/json"}
        if session:
            headers["Mcp-Session-Id"] = session
        message = {"jsonrpc": "2.0", "id": request_id, "result": result}
        raw = json.dumps(message, separators=(",", ":"))
        if self.sse:
            # Split a JSON object across data lines at a legal whitespace position.
            raw = raw.replace(',"result":', ',\n"result":', 1)
            body = b": heartbeat\n\n" + b"\n".join(b"data: " + part.encode() for part in raw.splitlines()) + b"\n\n"
        else:
            body = raw.encode()
        return status, headers, body

    @staticmethod
    def result(payload):
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}

    @staticmethod
    def error(code):
        return {"isError": True, "content": [{"type": "text", "text": json.dumps({"code": code})}]}

    def __call__(self, method, url, headers, body, timeout):
        message = json.loads(body)
        method_name = message["method"]
        params = message.get("params", {})
        name = params.get("name", method_name)
        args = params.get("arguments", params)
        self.calls.append(RequestRecord(name, copy.deepcopy(args), len(json.dumps(args).encode())))
        auth = headers.get("Authorization", "")
        if auth.removeprefix("Bearer ") in self.revoked_keys:
            return 401, {}, b""
        if self.expire_next and method_name != "initialize":
            self.expire_next = False
            self.sessions.clear()
            return 404, {}, b""
        if self.faults[name]:
            fault = self.faults[name].popleft()
            if isinstance(fault, Exception):
                raise fault
            if isinstance(fault, int):
                return fault, {}, b""
            if isinstance(fault, tuple):
                return fault
            return self._reply(message.get("id"), fault)
        if method_name == "initialize":
            self.session_counter += 1
            session = f"fake-session-{self.session_counter}"
            self.sessions.add(session)
            self.protocol_versions.append(params["protocolVersion"])
            return self._reply(message.get("id"), {"protocolVersion": params["protocolVersion"], "capabilities": {}, "serverInfo": {"name": "box-fake", "version": "1"}}, session=session)
        if headers.get("Mcp-Session-Id") not in self.sessions:
            return 404, {}, b""
        if method_name == "notifications/initialized":
            self.initialized_notifications += 1
            return 202, {}, b""
        if method_name == "tools/list":
            definitions = []
            for tool in sorted(self.tools):
                props = {"box": {"type": "string"}} if tool in {"box_status", "box_wake", "box_hibernate"} else {}
                if tool == "box_wake":
                    props["wait"] = {"type": "boolean"}
                definitions.append({"name": tool, "inputSchema": {"type": "object", "properties": props, "required": list(props)}})
            return self._reply(message["id"], {"tools": definitions})
        if method_name != "tools/call":
            return 400, {}, b""
        return self._reply(message["id"], self._tool(name, args))

    def _file(self, path):
        if not isinstance(path, str) or not path.startswith("/workspace/"):
            raise ValueError("fake path outside workspace")
        p = self.root / path.removeprefix("/workspace/")
        if not p.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("fake path outside workspace")
        return p

    def _tool(self, name, args):
        if name == "box_list":
            return self.result({"boxes": self.machines, "max_boxes": 4})
        if name == "usage":
            return self.result({"today": {"cost_micros": self.cost_today}, "month": {"cost_micros": self.cost_month}})
        if name == "billing":
            return copy.deepcopy(self.billing)
        if name == "box_status":
            if self.wake_started is not None and self.clock() - self.wake_started >= self.wake_delay:
                self.state = "running"
            return self.result({"state": self.state, "image": "fake-image", "upgrade_available": True, "last_saved_at_ms": 0})
        if name == "box_wake":
            self.wake_started = self.clock()
            return self.result({"accepted": True})
        if name == "box_hibernate":
            self.state, self.wake_started = "hibernated", None
            return self.result({"state": self.state})
        if name == "write_file":
            data = args["content"].encode()
            if self.disk_full:
                return self.error("disk_full")
            if self.write_limit is not None and len(data) > self.write_limit:
                return self.error("too_large")
            path = self._file(args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return self.result({"written": len(data)})
        if name == "read_file":
            return self.result({"content": self._file(args["path"]).read_text()})
        if name == "list_dir":
            return self.result({"entries": [p.name for p in self._file(args["path"]).iterdir()]})
        if name == "screenshot":
            return {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(self.image).decode()}]}
        if name == "computer_state":
            return {"structuredContent": {"windows": self.windows}, "content": [] if self.desktop_image is None else [
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(self.desktop_image).decode()}]}
        if name == "browser_snapshot":
            return self.result({"elements": self.elements})
        if name == "browser_navigate":
            return self.result({"ok": True})
        if name == "shell":
            if self.state != "running":
                return self.error("asleep")
            return self._shell(args["command"])
        return self.result({"ok": True})

    def _shell(self, command):
        if self.reset_on_shell:
            self.reset_on_shell = False
            if not self.child_survives_reset:
                for job in self.jobs.values():
                    job["alive"] = False
            self.sessions.clear()
            raise TimeoutError("fake reset")
        argv = shlex.split(command)
        if "/workspace/ax/bin/ax-sbx-run" in argv:
            index = argv.index("/workspace/ax/bin/ax-sbx-run")
            sub, *args = argv[index + 1:]
            if sub == "version":
                return self.result({"stdout": self.runner_version, "rc": 0})
            if sub == "selfcheck":
                checks = dict(self.checks)
                if args:
                    checks["scheduled-publisher"] = "ok" if self.eigen_cli and self.eigen_id else {"status": "fail", "reason": "id_missing" if self.eigen_cli else "cli_missing"}
                return self.result({"checks": checks, "disk": {"workspace_free_mib": self.disk_free_mib}, "tools": {"flock": self.flock, "setsid": self.setsid}})
            if sub == "busy":
                return self.result({"busy": self.busy})
            if sub == "status":
                return self.result(self.jobs.get(args[0], {"state": "not_found"}))
            if sub == "lookup":
                matches = [dict(job, job_id=ident) for ident, job in self.jobs.items() if job.get("idempotency_key") == args[0]]
                return self.result(matches[0] if matches else {"state": "not_found"})
            if sub == "cancel":
                self.jobs[args[0]].update(state="cancelled", rc=143, alive=False)
                return self.result(self.jobs[args[0]])
            if sub == "logs":
                return self.result({"stdout": "fake log", "stderr": "", "since": int(args[1])})
            if sub == "fetch":
                job, name, offset, length = args
                if self.truncate_fetch_above is not None and int(length) > self.truncate_fetch_above:
                    return self.result({"stdout": "cut", "truncated": True})
                data = self.artifacts[(job, name)][int(offset):int(offset) + int(length)]
                return self.result({"data": base64.b64encode(data).decode()})
        if not self.execute_shell:
            return self.error("shell_executor_disabled")
        # Explicit opt-in only. A clean environment prevents inherited credentials.
        translated = command.replace("/workspace/", str(self.root) + "/")
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root)}
        completed = subprocess.run(["bash", "-c", translated], cwd=self.root, env=env, capture_output=True, timeout=30)
        text = completed.stdout.decode(errors="replace")
        if len(text.encode()) > self.truncate_at:
            return self.result({"stdout": text[:1000] + "[truncated]" + text[-1000:], "rc": completed.returncode, "truncated": True})
        return self.result({"stdout": text, "rc": completed.returncode})


FakeBox = BoxFake
FakeBoxMCP = BoxFake

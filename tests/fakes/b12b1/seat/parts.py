"""席の試験用の部品。ソケットも外部サービスも使わない。"""
from __future__ import annotations

import asyncio
from io import BytesIO
import json
import socket
import struct


class FakeConnection:
    family = socket.AF_UNIX

    def __init__(self, uid=1234, request=b""):
        self.uid, self.request = uid, request
        self.closed = False
        self.output = BytesIO()

    def getsockopt(self, *args):
        return struct.pack("3i", 1, self.uid, 2)

    def settimeout(self, timeout):
        self.timeout = timeout

    def makefile(self, mode):
        if mode == "rb":
            return BytesIO(self.request)
        return RetainedOutput(self.output)

    def shutdown(self, *args):
        self.closed = True

    def close(self):
        self.closed = True


class RetainedOutput:
    def __init__(self, output):
        self.output = output

    def write(self, data):
        return self.output.write(data)

    def flush(self):
        pass

    def close(self):
        pass


class FakeEffects:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.pending = {"seat-one": ["approval-a"], "seat-two": ["approval-b"]}
        self.claims = {"seat-one": ["claim-a"], "seat-two": ["claim-b"]}
        self.controls = {"seat-one": ["command-a"], "seat-two": ["command-b"]}

    def action(self, name):
        def apply(principal, reason):
            if name in self.fail:
                raise OSError("fake failure")
            self.calls.append((name, principal, reason))
            for action, state in (("deny_pending", self.pending), ("release_controls", self.controls), ("release_claims", self.claims)):
                if name == action:
                    state[principal] = []
        return apply

    def mapping(self):
        return {name: self.action(name) for name in ("deny_pending", "release_controls", "release_claims", "close_sessions", "seat_sync", "notify_suspended")}


def request(app, *, ident=None, via=None, body=None, method="POST", path="/v1/instructions", headers=None, **extra):
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "server": ("fake", 8082), "client": ("fake", 1), "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
             "ax_ident": ident, **extra}
    if via:
        scope["_seat_via"] = via
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    output = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(output) if output else None

"""CLI と中継用の偽。実際のポートとソケットを作らない。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlsplit


class FakeEntry:
    target = "fake-commander.sock"

    def __init__(self, status=200, response=None, error=None):
        self.status = status
        self.response = response if response is not None else {"control": {"ok": True, "data": []}}
        self.error = error
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.error:
            raise self.error
        return self.status, self.response


class FakeConnection:
    def __init__(self, response=None, connect_error=None, response_error=None):
        self.response = response or {"ok": True}
        self.connect_error, self.response_error = connect_error, response_error
        self.calls, self.connects, self.closed = [], 0, 0

    def connect(self):
        self.connects += 1
        if self.connect_error:
            raise self.connect_error

    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body, headers))

    def getresponse(self):
        if self.response_error:
            raise self.response_error
        return SimpleNamespace(status=200, read=lambda size: json.dumps(self.response).encode())

    def close(self):
        self.closed += 1


class FakeCore:
    def __init__(self):
        self.calls = []
        self.down = set()

    def __call__(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        self.calls.append((method, u, headers, body, timeout))
        if u.hostname in self.down:
            raise ConnectionError("fake")
        return 200, {}, json.dumps({"path": u.path, "query": u.query}).encode()


async def request_app(app, path, *, method="GET", ident=None, query=""):
    """ASGI を直接呼ぶ。TestClient もソケットも使わない。"""
    messages = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "scheme": "http", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": query.encode(), "root_path": "", "headers": [],
             "server": ("fake", 0), "client": ("fake", 0), "ax_ident": ident}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], json.loads(raw) if raw else None


def request(app, path, **kwargs):
    return asyncio.run(request_app(app, path, **kwargs))

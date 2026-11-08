"""starlette のアプリを ASGI で直接呼ぶ小さな道具（httpx に頼らない）。"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit


def call(app, method: str, url: str, *, body=None, headers: dict | None = None) -> tuple[int, dict | None]:
    u = urlsplit(url)
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if raw:
        hdrs.append((b"content-type", b"application/json"))
        hdrs.append((b"content-length", str(len(raw)).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": u.path,
             "raw_path": u.path.encode(), "query_string": u.query.encode(), "headers": hdrs, "server": ("test", 80), "client": ("127.0.0.1", 1)}
    out: dict = {"status": None, "body": b""}
    sent = {"done": False}

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    asyncio.run(app(scope, receive, send))
    try:
        parsed = json.loads(out["body"].decode("utf-8")) if out["body"] else None
    except ValueError:
        parsed = None
    return out["status"], parsed

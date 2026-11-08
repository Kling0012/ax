"""偽の HTTP 転送。axcommon.client の Transport と同じ形の呼び出し可能な物。ソケットを使わない。

  t = FakeTransport()
  t.add("POST", "/v1/events", status=200, json={"ok": True})
  t.add("GET", "/v1/todos", handler=lambda call: (200, {}, b"[]"))
  t.down()            # 以後は接続できない（Unavailable）
  t.up()
  t.fail_next(2)      # 次の 2 回は 503
  t.calls             # 記録（method、url、path、headers、json）
応答の登録が無い (method, path) は 404 を返す。登録は後勝ち。
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from axcommon.client import Unavailable


@dataclass
class Call:
    method: str
    url: str
    path: str
    query: str
    headers: dict
    body: bytes | None

    @property
    def json(self) -> Any:
        return _json.loads(self.body.decode("utf-8")) if self.body else None


class FakeTransport:
    def __init__(self):
        self.calls: list[Call] = []
        self._routes: list[tuple[str, str, Callable[[Call], tuple]]] = []
        self._down = False
        self._fail = 0

    def add(self, method: str, path: str, *, status: int = 200, json: Any = None, body: bytes | None = None, handler: Callable | None = None):
        if handler is None:
            raw = body if body is not None else (_json.dumps(json).encode("utf-8") if json is not None else b"")

            def handler(call, _s=status, _r=raw):  # noqa: E306
                return _s, {"Content-Type": "application/json"}, _r

        self._routes.append((method.upper(), path, handler))
        return self

    def down(self):
        self._down = True

    def up(self):
        self._down = False

    def fail_next(self, n: int = 1):
        self._fail = n

    def paths(self, method: str | None = None) -> list[str]:
        return [c.path for c in self.calls if method is None or c.method == method]

    def __call__(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        call = Call(method, url, u.path, u.query, dict(headers), body)
        self.calls.append(call)
        if self._down:
            raise Unavailable("ConnectError")
        if self._fail > 0:
            self._fail -= 1
            return 503, {}, b'{"error":{"code":"unavailable","message":"fake"}}'
        for m, p, h in reversed(self._routes):
            if m == method.upper() and p == u.path:
                return h(call)
        return 404, {"Content-Type": "application/json"}, b'{"error":{"code":"not_found","message":"fake"}}'

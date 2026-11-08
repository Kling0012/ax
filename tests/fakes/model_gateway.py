"""偽のモデルの入口（D-15 の「偽のモデルの入口」。決まった応答・429・遅延を返す）。ソケットを使わない。

本物のゲートウェイ（F-7）の使う面だけを真似る: POST /v1/messages、GET /v1/models。認証は Authorization: Bearer か x-api-key。
axcommon.client の Transport として使える（ApiClient(transport=gw)）。Starlette があれば asgi_app() で ASGI アプリにもなる（TestClient 用）。

  gw = FakeModelGateway(api_key="dummy-key")
  gw.script(Reply("こんにちは"), RateLimit(), Delay(30, Reply("遅い応答")), ServerError(503), Quota())
  # script が空になったら default（既定は Reply("ok")）を返す。script は先頭から 1 件ずつ使う。
  gw.requests    # 受けた要求（model、messages など。鍵は残さない）
Delay は clock.sleep(秒) を呼ぶ（FakeClock なら時計が進むだけで待たない）。clock が無ければ記録だけ。
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


@dataclass
class Reply:
    text: str = "ok"
    model: str | None = None
    input_tokens: int = 10
    output_tokens: int = 5


@dataclass
class RateLimit:
    retry_after_s: int | None = None


@dataclass
class Quota:
    pass


@dataclass
class ServerError:
    status: int = 503


@dataclass
class Delay:
    seconds: float
    then: Any = None  # 遅延の後に返す振る舞い。None なら default


@dataclass
class Unauthorized:
    pass


@dataclass
class _Req:
    path: str
    model: str | None
    messages: list = field(default_factory=list)
    system: Any = None
    max_tokens: int | None = None


class FakeModelGateway:
    def __init__(self, api_key: str | None = "dummy-key", models: list[str] | None = None, clock: Any = None, default: Any = None):
        self.api_key = api_key
        self.models = models or ["fake/model-a", "fake/model-b"]
        self.clock = clock
        self.default = default if default is not None else Reply("ok")
        self._script: deque = deque()
        self.requests: list[_Req] = []
        self.delays: list[float] = []
        self.unauthorized_count = 0

    def script(self, *behaviors):
        self._script.extend(behaviors)
        return self

    # --- 認証 ---
    def _authorized(self, headers: dict) -> bool:
        if self.api_key is None:
            return True
        h = {k.lower(): v for k, v in (headers or {}).items()}
        got = None
        if h.get("authorization", "").lower().startswith("bearer "):
            got = h["authorization"].split(None, 1)[1].strip()
        elif "x-api-key" in h:
            got = h["x-api-key"].strip()
        return got == self.api_key

    # --- 応答の組み立て ---
    @staticmethod
    def _json(status: int, obj: dict, headers: dict | None = None) -> tuple[int, dict, bytes]:
        return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def _behave(self, b: Any, req_model: str | None) -> tuple[int, dict, bytes]:
        if isinstance(b, Delay):
            self.delays.append(b.seconds)
            if self.clock is not None:
                self.clock.sleep(b.seconds)
            return self._behave(b.then if b.then is not None else self.default, req_model)
        if isinstance(b, RateLimit):
            hdr = {"Retry-After": str(b.retry_after_s)} if b.retry_after_s is not None else {}
            return self._json(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit exceeded"}}, hdr)
        if isinstance(b, Quota):
            return self._json(429, {"type": "error", "error": {"type": "insufficient_quota", "message": "quota exceeded for this plan"}})
        if isinstance(b, ServerError):
            return self._json(b.status, {"type": "error", "error": {"type": "api_error", "message": "upstream down"}})
        if isinstance(b, Unauthorized):
            return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid key"}})
        if isinstance(b, Reply):
            return self._json(200, {
                "id": "msg_fake", "type": "message", "role": "assistant", "model": b.model or req_model or "fake/model-a",
                "content": [{"type": "text", "text": b.text}], "stop_reason": "end_turn",
                "usage": {"input_tokens": b.input_tokens, "output_tokens": b.output_tokens},
            })
        raise TypeError(f"未知の振る舞い: {b!r}")

    def handle(self, method: str, path: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
        method = method.upper()
        if method == "GET" and path == "/healthz":
            return self._json(200, {"ok": True, "component": "fake-model-gateway", "version": "0"})
        if not self._authorized(headers):
            self.unauthorized_count += 1
            return self._behave(Unauthorized(), None)
        if method == "GET" and path == "/v1/models":
            return self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in self.models]})
        if method == "POST" and path == "/v1/messages":
            try:
                req = json.loads((body or b"{}").decode("utf-8"))
            except ValueError:
                return self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "not json"}})
            self.requests.append(_Req(path, req.get("model"), req.get("messages", []), req.get("system"), req.get("max_tokens")))
            b = self._script.popleft() if self._script else self.default
            return self._behave(b, req.get("model"))
        return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "no such route"}})

    # --- axcommon.client の Transport ---
    def __call__(self, method, url, headers, body, timeout):
        return self.handle(method, urlsplit(url).path, headers, body)

    # --- ASGI（Starlette があるとき。TestClient で使う）---
    def asgi_app(self):
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route

        async def endpoint(request):
            status, headers, raw = self.handle(request.method, request.url.path, dict(request.headers), await request.body())
            return Response(raw, status_code=status, headers=headers)

        return Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST"])])

"""healthz 付きの小さなアプリの雛形（D-2.2 axcommon.http、S-2.1）。Starlette。

create_app() が付けるもの:
  - GET /healthz  {"ok":true,"component":"<名前>","version":"<版>"}（S-2.1）。extra_health() の項目と、拡張の読み込みに失敗した名前 ext_failed を足せる。
  - health.beat  起動時に 1 回、以後 heartbeat_s ごと（S-2.1、S-17。既定 60）に時系列記録へ。uptime_s と lag_s（予定の間隔からのずれ）。
    送れなければ outbox に溜める（EventSink の役目）。beat が失敗しても部品を止めない。
  - 例外の変換  {"error":{"code":…,"message":…}}（schemas/api-error.schema.json）。内部の詳細（スタックなど）は返さない。
  - 拡張  ext_dir にある <束の ID>.py の register(app) を読み込む（axcommon.ext）。
app.state に heartbeat（Heartbeat）、sink、ext_results を置く。試験は app.state.heartbeat.beat() を直接呼べる（待たない）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import traceback
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import ext as ext_mod
from .events import EventSink

log = logging.getLogger("axcommon.http")
DEFAULT_HEARTBEAT_S = 60


def json_error(status: int, code: str, message: str = "", hint: str | None = None) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if hint:
        err["hint"] = hint
    return JSONResponse({"error": err}, status_code=status)


async def read_json(request: Request, schema: str | None = None, fragment: str | None = None) -> tuple[Any, JSONResponse | None]:
    """本文を JSON として読み、schema（schemas/ の名前）があれば検査する。(データ, エラー応答) を返す。
    JSON でなければ 400 bad_json、スキーマに合わなければ 422 bad_request（S-6.5）。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return None, json_error(400, "bad_json", "本文が JSON でない")
    if schema:
        from . import schemas

        errs = schemas.errors(schema, data, fragment)
        if errs:
            return None, json_error(422, "bad_request", "; ".join(errs[:5]))
    return data, None


class Heartbeat:
    def __init__(self, sink: EventSink | None, component: str, interval_s: float, *, monotonic: Callable[[], float] = time.monotonic):
        self.sink, self.component, self.interval_s = sink, component, float(interval_s)
        self._mono = monotonic
        self.started = monotonic()
        self._last: float | None = None
        self.beats = 0

    def beat(self) -> dict | None:
        """health.beat を 1 回送る。失敗しても例外を出さない。"""
        now = self._mono()
        lag = 0.0 if self._last is None else max(0.0, (now - self._last) - self.interval_s)
        self._last = now
        self.beats += 1
        if self.sink is None:
            return None
        try:
            ev = self.sink.emit("health.beat", self.component, {"uptime_s": round(now - self.started, 3), "lag_s": round(lag, 3)})
            return ev
        except Exception:  # noqa: BLE001  beat の失敗で部品を止めない
            log.exception("health.beat を作れない")
            return None

    async def run(self, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        while True:
            await sleep(self.interval_s)
            await asyncio.to_thread(self.beat)
            if self.sink is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.sink.flush)


def _heartbeat_interval(explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    try:
        from .defaults import Defaults

        return float(Defaults.load().get("heartbeat_s"))
    except Exception:  # noqa: BLE001  config が読めなくても動く（fail-soft）
        return float(DEFAULT_HEARTBEAT_S)


def create_app(component: str, version: str, *, routes: list | None = None, sink: EventSink | None = None,
               heartbeat_s: float | None = None, extra_health: Callable[[], dict] | None = None,
               ext_dir: str | None = None, on_startup: list[Callable[[], Any]] | None = None,
               on_shutdown: list[Callable[[], Any]] | None = None, run_heartbeat_loop: bool = True) -> Starlette:
    hb = Heartbeat(sink, component, _heartbeat_interval(heartbeat_s))

    async def healthz(request: Request) -> JSONResponse:
        body: dict[str, Any] = {"ok": True, "component": component, "version": version}
        if extra_health is not None:
            try:
                body.update(extra_health())
            except Exception:  # noqa: BLE001
                body["extra_health_error"] = True
        failed = getattr(request.app.state, "ext_failures", None)
        if failed:
            body["ext_failed"] = list(failed)
        return JSONResponse(body)

    async def on_exc(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            code = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed", 413: "too_large",
                    422: "bad_request", 429: "rate_limited"}.get(exc.status_code, "http_error")
            return json_error(exc.status_code, code, str(exc.detail))
        # 例外の文には秘密が入りうるので、型の名前とフレームの位置だけを残す
        log.error("未処理の例外: %s\n%s", type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__)))
        return json_error(500, "internal", "内部エラー")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        for fn in on_startup or []:
            r = fn()
            if asyncio.iscoroutine(r):
                await r
        await asyncio.to_thread(hb.beat)  # 起動時の beat
        task = asyncio.create_task(hb.run()) if run_heartbeat_loop else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for fn in on_shutdown or []:
                r = fn()
                if asyncio.iscoroutine(r):
                    await r

    all_routes = [Route("/healthz", healthz, methods=["GET"])] + list(routes or [])
    app = Starlette(routes=all_routes, lifespan=lifespan, exception_handlers={Exception: on_exc, HTTPException: on_exc})
    app.state.heartbeat = hb
    app.state.sink = sink
    app.state.ext_failures = []
    app.state.component = component
    app.state.ext_results = ext_mod.load_extensions(app, ext_dir)
    return app

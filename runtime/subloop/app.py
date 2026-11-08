"""サブループの HTTP（S-4.4、S-7.10）。`/healthz`（axcommon.http）、`/status`、`/apply`、`POST /items`（accepts_items のサブループだけ）。

- `/apply` と `/items` は control の資格 `control-apply-token` だけを受ける（F-18.4）。ほかの網には届かない（S-15.5）。
- `/items` の受け取り（S-7.10）: acceptance の examples と boundaries が両方あること（無ければ 400）。同じ item_key は 1 回だけ受け付ける（冪等）。
  inbox が inbox_max に達していれば 429（inbox_full）。受け付けたら item.state（planned、data.todo_id）を書く。
  mode が run なら受け取りの直後に run を作る。draining・collect_only・停止中は保存だけして、run に戻ったら拾う（S-5.2）。
- 定期: scheduler の tick、control の effective の読み（control_poll_s ごと。/apply が届かなくても合わせる）、outbox の再送。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import creds, timeutil
from axcommon.http import create_app, json_error, read_json

from .executor import Executor
from .inbox import accept_item

log = logging.getLogger("ax.subloop.app")
MODES = ("run", "collect_only", "poll_paused", "draining")


def _authorized(request: Request, apply_token: str | None) -> bool:
    if not apply_token:
        return False  # 資格が無いときは全拒否（fail-closed）
    got = creds.parse_bearer(request.headers.get("authorization"))
    return bool(got) and hmac.compare_digest(got, apply_token)


def build_app(ex: Executor, *, apply_token: str | None, ext_dir: str | None = None, run_loops: bool = True, tick_s: float = 2.0, graph_errors: list | None = None):
    sink = ex.svc.sink
    loops: list[asyncio.Task] = []
    ready = not run_loops
    if run_loops:
        ex.mode = "unknown"

    async def status(request: Request) -> JSONResponse:
        s = ex.status()
        if graph_errors:
            s["graph_errors"] = graph_errors
        return JSONResponse(s)

    async def apply(request: Request) -> JSONResponse:
        if not _authorized(request, apply_token):
            return json_error(401, "unauthorized", "control の資格が要る")
        data, err = await read_json(request)
        if err is not None:
            return err
        if not isinstance(data, dict) or data.get("mode") not in MODES or not isinstance(data.get("revision"), int):
            return json_error(422, "bad_request", "mode と revision が要る")
        knobs = data.get("knobs")
        if knobs is not None and not isinstance(knobs, dict):
            return json_error(422, "bad_request", "knobs は辞書")
        applied = ready and ex.apply(data["mode"], knobs, data["revision"], run_now=bool(data.get("run_now")))
        return JSONResponse({"ok": True, "applied": applied, "revision": ex.revision})

    async def items(request: Request) -> JSONResponse:
        if not _authorized(request, apply_token):
            return json_error(401, "unauthorized", "control の資格が要る")
        data, err = await read_json(request)
        if err is not None:
            return err
        code, body = accept_item(ex, data)
        return JSONResponse(body, status_code=code)

    routes = [Route("/status", status, methods=["GET"]), Route("/apply", apply, methods=["POST"])]
    if ex.manifest.get("accepts_items"):
        routes.append(Route("/items", items, methods=["POST"]))
    if ex.svc.plugin_id == "release":
        from plugins.subloop.release.core.http import delivery_routes
        routes.extend(delivery_routes(getattr(ex.svc, "release_service", None),
                                      change_token=creds.read_secret_optional("plugin-token-change"),
                                      apply_token=apply_token))

    async def loop_tick() -> None:
        nonlocal ready
        last_poll = -1e9
        poll_s = float(ex.svc.defaults.get("control_poll_s"))
        while True:
            try:
                if ready:
                    await ex.tick()
                now = asyncio.get_event_loop().time()
                if now - last_poll >= poll_s:
                    last_poll = now
                    try:
                        eff = await asyncio.to_thread(ex.svc.control.effective, ex.svc.plugin_id)
                        if (isinstance(eff, dict) and eff.get("mode") in MODES
                                and type(eff.get("revision")) is int
                                and (eff.get("knobs") is None or isinstance(eff.get("knobs"), dict))
                                and ex.apply(eff["mode"], eff.get("knobs"), eff["revision"])):
                            if not ready:
                                await ex.recover()
                                ready = True
                    except Exception as e:  # noqa: BLE001  control に届かなくても動き続ける
                        log.warning("control の effective を読めない: %s", type(e).__name__)
                    await asyncio.to_thread(sink.flush)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("tick の失敗: %s", type(e).__name__)
            await asyncio.sleep(tick_s)

    async def on_startup() -> None:
        if run_loops:
            loops.append(asyncio.create_task(loop_tick()))
        else:
            try:
                await ex.recover()
            except Exception as e:  # noqa: BLE001
                log.error("recover の失敗: %s", type(e).__name__)

    async def on_shutdown() -> None:
        for t in loops:
            t.cancel()
        for t in loops:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def extra_health() -> dict[str, Any]:
        return {"mode": ex.mode, "inbox_queued": ex.store.inbox_queued()}

    return create_app(ex.svc.plugin_id, str(ex.manifest.get("version", "0")), routes=routes, sink=sink, heartbeat_s=float(ex.manifest.get("health", {}).get("heartbeat_s", 60)),
                      extra_health=extra_health, ext_dir=ext_dir, on_startup=[on_startup], on_shutdown=[on_shutdown])

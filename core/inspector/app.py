"""点検係の組み立てと HTTP の口。ax-core の 8080 に /healthz と読み取りの GET /v1/inspection。

利用者による変更の記録経路は D-12 に API の契約がない。既存の /v1/review-changes は
501 を返す保留の口とし、推測した契約で利用者の変更を受け付けない（build/OPEN.md）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from axcommon.client import TimelineClient
from axcommon.events import EventSink

from . import COMPONENT, VERSION
from .config import InspectionConfig
from .inspector import Inspector
from .state import StateStore

log = logging.getLogger("ax.inspector")
TICK_S = 30.0


def build_inspector(cfg: InspectionConfig | None = None, *, clients=None, sink: EventSink | None = None) -> Inspector:
    from axcommon.creds import read_secret_optional

    from core.manager.clients import Clients, ManagerControl

    cfg = cfg or InspectionConfig.load()
    if clients is None:
        clients = Clients(timeline=TimelineClient(cfg.urls["timeline"]),
                          control=ManagerControl(cfg.urls["control"], read_secret_optional(cfg.control_token_name)), ledger=None, pool=None)
    sink = sink or EventSink(clients.timeline, COMPONENT, cfg.host, os.path.join(cfg.state_dir, "outbox"))
    return Inspector(clients, StateStore(cfg.state_dir), sink, cfg)


def create_inspector_app(insp: Inspector, *, ext_dir: str | None = None, run_loop: bool = True):
    from axcommon.http import create_app, json_error
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    holder: dict = {"task": None}

    async def loop() -> None:
        while True:
            try:
                await asyncio.to_thread(insp.run_once)
            except Exception as e:  # noqa: BLE001  1 回の失敗で止めない
                log.error("点検の周期の失敗: %s", type(e).__name__)
            await asyncio.sleep(TICK_S)

    async def view(request):
        return JSONResponse(insp.view())

    async def review_changes(request):
        # D-12 は変更前後と理由の記録を要求するが、送り手・資格・API を規定していない。
        return json_error(501, "not_implemented", "利用者変更の受け渡し契約は未確定（D-12、build/OPEN.md）")

    async def start() -> None:
        if run_loop:
            holder["task"] = asyncio.create_task(loop())

    async def stop() -> None:
        t = holder["task"]
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    app = create_app(COMPONENT, VERSION, routes=[Route("/v1/inspection", view, methods=["GET"]),
                                                  Route("/v1/review-changes", review_changes, methods=["POST"])],
                     sink=insp.sink, extra_health=lambda: {"phase": insp.state["phase"], "next_at": insp.state["next_at"]},
                     ext_dir=ext_dir, on_startup=[start], on_shutdown=[stop])
    app.state.inspector = insp
    return app

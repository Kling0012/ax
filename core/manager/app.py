"""管理役の組み立てと HTTP の口。中核の網（ax-core）の 8080 に /healthz と読み取りの /v1/manager だけを持つ（書き込みの口は持たない）。

周回は背景のタスクで回す（購読の長い待ち → 契機があれば周回、無くても manager_tick_min で周回）。
1 回の周回の失敗は次の周回を止めない。時系列記録に届かなくても動き続ける（S-2.2。事象は outbox/ に溜める）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from axcommon import ext as ext_mod
from axcommon.client import TimelineClient
from axcommon.events import EventSink

from . import COMPONENT, VERSION
from .clients import build_clients
from .config import ManagerConfig
from .cycle import Manager
from .llm import PoolLLM
from .store import Store

log = logging.getLogger("ax.manager")


def build_manager(cfg: ManagerConfig | None = None, *, clients=None, redact=None, llm=None, store: Store | None = None,
                  sink: EventSink | None = None) -> Manager:
    cfg = cfg or ManagerConfig.load()
    clients = clients or build_clients(cfg)
    state = cfg.state_dir
    store = store or Store(os.path.join(state, "manager.sqlite3"))
    sink = sink or EventSink(clients.timeline if isinstance(clients.timeline, TimelineClient) else None, COMPONENT, cfg.host,
                             os.path.join(state, "outbox"))
    if redact is None:
        try:
            from axcommon.redact import from_environment

            redact = from_environment().as_hook()
        except Exception:  # noqa: BLE001  秘密検査が組めないときは、既定の形の検査だけで続ける
            log.error("秘密検査を組めなかった。既定の検査にする")
            from axcommon.redact import Redactor

            redact = Redactor().as_hook()
    llm = llm or PoolLLM(clients.pool, role="manager", poll_s=cfg.poll_s)
    return Manager(clients, store, sink, cfg, llm, outbox_dir=os.path.join(state, "ledger-outbox"), redact=redact)


def create_manager_app(mgr: Manager, *, ext_dir: str | None = None, run_loop: bool = True):
    from axcommon.http import create_app
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    holder: dict = {"task": None}

    async def loop() -> None:
        while True:
            try:
                await asyncio.to_thread(mgr.fetch, mgr.cfg.longpoll_s if mgr.timeline_ok else 0.0)
                trig = mgr.due()
                if trig:
                    await asyncio.to_thread(mgr.run_cycle, trig)
            except Exception as e:  # noqa: BLE001  周回の失敗で止めない
                log.error("周回の失敗: %s", type(e).__name__)
            if not mgr.timeline_ok:
                await asyncio.sleep(5.0)     # 時系列記録が落ちている間は、空回りしないよう間を置く（周回は止めない）

    async def status(request):
        return JSONResponse({"cycles": mgr.cycles, "last_cycle_at": mgr.last_cycle_at, "last_tick_at": mgr.last_tick_at,
                             "timeline_ok": mgr.timeline_ok, "inbox": mgr.store.counts(),
                             "bad_streak": int(mgr.store.meta("bad_streak", "0") or 0), "decisions": mgr.store.decisions(10)})

    async def start() -> None:
        if run_loop:
            holder["task"] = asyncio.create_task(loop())

    async def stop() -> None:
        t = holder["task"]
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    app = create_app(COMPONENT, VERSION, routes=[Route("/v1/manager", status, methods=["GET"])], sink=mgr.sink,
                     extra_health=lambda: {"cycles": mgr.cycles, "last_cycle_at": mgr.last_cycle_at, "timeline_ok": mgr.timeline_ok},
                     ext_dir=ext_dir, on_startup=[start], on_shutdown=[stop])
    app.state.manager = mgr
    return app

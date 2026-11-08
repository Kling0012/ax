"""オブザーバーの組み立て。中核の網（ax-core）の 8080 に /healthz だけを持つ（外へ API を出さない）。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from axcommon import ext as ext_mod
from axcommon.client import LedgerClient, TimelineClient
from axcommon.events import EventSink
from axcommon.creds import read_secret_optional

from . import COMPONENT, VERSION
from .observer import Observer
from .settings import Settings

log = logging.getLogger("observer")


def build_observer(settings: Settings | None = None, *, timeline: TimelineClient | None = None, ledger: LedgerClient | None = None,
                   redact=None, sources=None) -> Observer:
    s = settings or Settings.from_env()
    timeline = timeline or TimelineClient(s.timeline_url)
    ledger = ledger if ledger is not None else LedgerClient(s.ledger_url, token=read_secret_optional("core-token-observer"))
    sink = EventSink(timeline, COMPONENT, s.host, os.path.join(s.data_dir, "outbox"))
    if redact is None:
        try:
            from axcommon.redact import from_environment

            r = from_environment({"AX_REDACT_INDEX": s.redact_index or "", "AX_PII_LIST": s.pii_list or "",
                                  **({"AX_CONFIG_DIR": os.environ["AX_CONFIG_DIR"]} if os.environ.get("AX_CONFIG_DIR") else {})})
            redact = r.as_hook()
        except Exception:  # noqa: BLE001  観測は続ける。取り込み時に再試行し、検査できない記録は送らない
            log.error("秘密検査を組めなかった")
            redact = None
    return Observer(s, timeline, sink, ledger=ledger, sources=sources, redact=redact)


def create_observer_app(obs: Observer, *, ext_dir: str | None = None, run_loop: bool = True):
    """starlette のアプリ。ext の register(app) は app.state.observer が入ってから呼ぶ。"""
    from axcommon.http import create_app

    state = {"task": None}

    async def loop() -> None:
        while True:
            try:
                await asyncio.to_thread(obs.run_once)
            except Exception as e:  # noqa: BLE001
                log.error("周期の失敗: %s", type(e).__name__)
            await asyncio.sleep(obs.s.observe_s)

    async def start() -> None:
        if run_loop:
            state["task"] = asyncio.create_task(loop())

    async def stop() -> None:
        t = state["task"]
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    app = create_app(COMPONENT, VERSION, sink=obs.sink, extra_health=obs.health_extra, ext_dir=None,
                     on_startup=[start], on_shutdown=[stop], run_heartbeat_loop=True)
    app.state.observer = obs
    ext_mod.load_extensions(app, ext_dir if ext_dir is not None else os.path.join(os.path.dirname(__file__), "ext"))
    return app

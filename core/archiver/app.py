"""archiver の常駐部分（healthz と 1 時間ごとの巡回）。設定は環境変数。

  AX_HOST                 事象の source.host、プロファイルの名前（/profile/<host>.yaml）
  AX_ARCHIVE_SOURCES      既定 /profile/archive-sources.yaml
  AX_PROFILE_FILE         既定 /profile/<AX_HOST>.yaml（archive_dest の取得）。AX_ARCHIVE_DEST_HOST/_PORT/_USER で上書き可
  AX_ARCHIVE_INTERVAL_S   巡回の周期（既定 3600）
  AX_ARCHIVE_FIRST_DELAY_S 起動後の最初の巡回までの待ち（既定 60）
  AX_ARCHIVE_VOL_BASE / AX_ARCHIVE_SRC_BASE   /vol、/src
  AX_STATE_DIR            /state（${ax_state_dir}。archive/verified/ と reap-enabled の置き場）
  AX_ARCHIVER_DB          既定 /data/archiver.sqlite
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from axcommon import client as axclient, events as axevents
from axcommon.http import create_app

from . import COMPONENT, VERSION, config
from .engine import Archiver, RunReport
from .guard import Guard
from .ledger import ArchiverLedger
from .notify import LedgerNotifier
from .rsync import Transport

log = logging.getLogger("archiver")


def build_archiver(environ: dict | None = None, *, emitter=None, transport: Transport | None = None) -> Archiver:
    env = os.environ if environ is None else environ
    host = env.get("AX_HOST", "unknown")
    try:
        from axcommon.defaults import Defaults
        age = int(Defaults.load().get("archive_after_days"))
    except Exception:  # noqa: BLE001
        age = 14
    sources = config.load_sources(env.get("AX_ARCHIVE_SOURCES", "/profile/archive-sources.yaml"), src_base=env.get("AX_ARCHIVE_SRC_BASE", "/src"),
                                  vol_base=env.get("AX_ARCHIVE_VOL_BASE", "/vol"), default_min_age_days=age)
    dest = config.load_dest(env.get("AX_PROFILE_FILE", f"/profile/{host}.yaml"), env)
    if emitter is None:
        emitter = axevents.EventSink(axclient.TimelineClient(axclient.service_url("timeline", 8080)), COMPONENT, host, "/data/outbox")
    ledger_client = axclient.LedgerClient(axclient.service_url("ledger", 8080))
    return Archiver(sources, transport or Transport(dest), ArchiverLedger(env.get("AX_ARCHIVER_DB", "/data/archiver.sqlite")), emitter,
                    state_dir=env.get("AX_STATE_DIR", "/state"), guard=Guard.from_environment(env), notifier=LedgerNotifier(ledger_client))


def build_app(archiver: Archiver, *, interval_s: float = 3600, first_delay_s: float = 60, sink=None, run_heartbeat_loop: bool = True):
    state = {"last_run_at": None, "last_failed": [], "runs": 0}
    task: dict = {}

    def run() -> RunReport:
        from axcommon import timeutil
        rep = archiver.run_once()
        state["last_run_at"] = timeutil.now_ts()
        state["last_failed"] = [s.name for s in rep.sources if s.failed]
        state["runs"] += 1
        flush = getattr(archiver.emitter, "flush", None)
        if flush:
            with contextlib.suppress(Exception):
                flush()
        return rep

    async def loop() -> None:
        await asyncio.sleep(first_delay_s)
        while True:
            try:
                await asyncio.to_thread(run)
            except Exception:  # noqa: BLE001  巡回の失敗で部品を止めない
                log.exception("巡回に失敗")
            await asyncio.sleep(interval_s)

    async def startup() -> None:
        task["t"] = asyncio.create_task(loop())

    async def shutdown() -> None:
        t = task.get("t")
        if t:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    app = create_app(COMPONENT, VERSION, sink=sink or archiver.emitter, run_heartbeat_loop=run_heartbeat_loop,
                     extra_health=lambda: {"last_run_at": state["last_run_at"], "last_failed": state["last_failed"], "runs": state["runs"],
                                           "reap_enabled": archiver.reap_enabled()},
                     on_startup=[startup], on_shutdown=[shutdown])
    app.state.run_once = run
    return app

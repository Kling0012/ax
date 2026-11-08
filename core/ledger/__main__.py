"""台帳の入口（`python -m core.ledger`）。1つのプロセスで中核の口（8080）と作業側の口（8081）を開く（F-18.4）。

環境変数: AX_STATE_DIR（既定 /state。SQLite とトークンの表）、AX_CORE_PORT（既定 8080）、AX_WORK_PORT（既定 8081）、
AX_TIMELINE_URL（既定 http://timeline:8080）、AX_HOST（事象の source.host）。
"""
from __future__ import annotations

import asyncio
import os

from axcommon.client import TimelineClient, service_url
from axcommon.events import EventSink


def build() -> tuple:
    from .app import create_core_app, create_work_app
    from .service import LedgerService
    from .store import Store
    from .watch import LedgerWatcher

    state = os.environ.get("AX_STATE_DIR", "/state")
    host = os.environ.get("AX_HOST", "unknown")
    timeline = TimelineClient(os.environ.get("AX_TIMELINE_URL") or service_url("timeline"))
    sink = EventSink(timeline, "ledger", host, os.path.join(state, "ledger", "outbox"))
    store = Store(os.path.join(state, "ledger", "ledger.sqlite3"))
    service = LedgerService(store, sink)
    core = create_core_app(service)
    work = create_work_app(service)
    watcher = LedgerWatcher(service, timeline)
    return core, work, service, watcher


def resolve_binds(environ: dict | None = None, addresses: list[str] | None = None) -> tuple[list[str], list[str]]:
    from core.timeline import netbind

    env = dict(os.environ if environ is None else environ)
    for mouth in ("CORE", "WORK"):
        env.pop(f"AX_TIMELINE_{mouth}_BIND", None)
        if value := env.get(f"AX_LEDGER_{mouth}_BIND"):
            env[f"AX_TIMELINE_{mouth}_BIND"] = value
    return netbind.resolve_binds(env, addresses=addresses)


async def main() -> None:
    import uvicorn
    from core.timeline import netbind

    core, work, service, watcher = build()
    core_port = int(os.environ.get("AX_CORE_PORT", "8080"))
    work_port = int(os.environ.get("AX_WORK_PORT", "8081"))
    core_hosts, work_hosts = resolve_binds()
    servers = [uvicorn.Server(uvicorn.Config(core, log_level="warning")).serve(
        sockets=netbind.bind_sockets(core_hosts, core_port))]
    if work_hosts:
        servers.append(uvicorn.Server(uvicorn.Config(work, log_level="warning")).serve(
            sockets=netbind.bind_sockets(work_hosts, work_port)))

    async def watch() -> None:
        await asyncio.to_thread(watcher.run)

    tasks = [asyncio.create_task(server) for server in servers] + [asyncio.create_task(watch())]
    try:
        await asyncio.gather(*tasks)
    finally:
        watcher.stop()


if __name__ == "__main__":
    asyncio.run(main())

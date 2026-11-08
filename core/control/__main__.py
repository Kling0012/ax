"""制御 API の入口（`python -m core.control`）。中核の口（8080）と作業側の口（8081）と10秒ごとの期限の走査（D-4.3）。

環境変数: AX_STATE_DIR（既定 /state）、AX_CORE_PORT（既定 8080）、AX_WORK_PORT（既定 8081）、
AX_REGISTRY（既定 /ax/plugins/registry.yaml）、AX_HOME（既定 /ax）、AX_HOST。
"""
from __future__ import annotations

import asyncio
import os
import logging

from axcommon.client import ApiClient, TimelineClient, service_url
from axcommon.creds import read_secret_optional
from axcommon.events import EventSink, set_redactor
from axcommon.redact import from_environment

SWEEP_INTERVAL_S = 10  # D-4.3: 10秒ごと


def build() -> tuple:
    from .app import create_core_app, create_work_app
    from .service import ControlService
    from .store import Store
    from .selection_exclusions import ExclusionAdapter, ResourcesExclusionClient

    set_redactor(from_environment().as_hook())
    state = os.environ.get("AX_STATE_DIR", "/state")
    host = os.environ.get("AX_HOST", "unknown")
    timeline = TimelineClient(os.environ.get("AX_TIMELINE_URL") or service_url("timeline"))
    sink = EventSink(timeline, "control", host, os.path.join(state, "control", "outbox"))
    store = Store(os.path.join(state, "control", "control.sqlite3"))
    try:
        token = read_secret_optional("core-token-control")
        if not token:
            raise ValueError("control credential unavailable")
        adapter = ExclusionAdapter(ResourcesExclusionClient(ApiClient(service_url("resources"), token=token)))
    except (TypeError, ValueError):
        logging.getLogger("core.control").warning("temporary exclusion client configuration invalid or control credential unavailable")
        adapter = None
    service = ControlService(store, sink, exclusion_adapter=adapter)
    core = create_core_app(service)
    work = create_work_app(service)
    return core, work, service


def resolve_binds(environ: dict | None = None, addresses: list[str] | None = None) -> tuple[list[str], list[str]]:
    from core.timeline import netbind

    env = dict(os.environ if environ is None else environ)
    for mouth in ("CORE", "WORK"):
        env.pop(f"AX_TIMELINE_{mouth}_BIND", None)
        if value := env.get(f"AX_CONTROL_{mouth}_BIND"):
            env[f"AX_TIMELINE_{mouth}_BIND"] = value
    return netbind.resolve_binds(env, addresses=addresses)


async def main() -> None:
    import uvicorn
    from core.timeline import netbind

    core, work, service = build()
    core_hosts, work_hosts = resolve_binds()
    core_port = int(os.environ.get("AX_CORE_PORT", "8080"))
    work_port = int(os.environ.get("AX_WORK_PORT", "8081"))
    core_srv = uvicorn.Server(uvicorn.Config(core, log_level="warning"))
    servers = [core_srv.serve(sockets=netbind.bind_sockets(core_hosts, core_port))]
    if work_hosts:
        work_srv = uvicorn.Server(uvicorn.Config(work, log_level="warning"))
        servers.append(work_srv.serve(sockets=netbind.bind_sockets(work_hosts, work_port)))

    async def sweep_loop() -> None:
        while not core_srv.started:
            await asyncio.sleep(0.1)
        while True:
            for sweep in (service.sweep_expiry, service.sweep_timers, service.retry_limits_registration, service.sync_limits):
                try:
                    await asyncio.to_thread(sweep)
                except Exception:
                    logging.getLogger("core.control").exception("期限の走査に失敗")
            await asyncio.sleep(SWEEP_INTERVAL_S)

    tasks = [asyncio.create_task(server) for server in servers] + [asyncio.create_task(sweep_loop())]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())

"""worker-pool のアプリの組み立て。設定は環境変数から読む。

  AX_HOST                   事象の source.host に入れるホスト名（profile の host）
  AX_WORKER_POOL_DB         既定 /data/worker-pool.sqlite
  AX_RESOURCES_URL          resources の URL（既定 http://resources:8080）
  AX_LAUNCHER_HOST          launcher（SSH 宛先）
  AX_WORKER_POOL_EXT_DIR    拡張（core/resources/ext の指先）
"""
from __future__ import annotations

import logging
import hmac
import asyncio
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from axcommon.http import create_app
from axcommon.client import ApiClient, service_url
from axcommon.defaults import Defaults

from . import COMPONENT, VERSION, api
from .jobs import JobsStore
from .launcher_client import LauncherClient
from axcommon.creds import TokenTable, parse_bearer, read_secret_optional
from starlette.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("worker-pool")


@dataclass
class Config:
    host: str = "unknown"
    db: str = "/data/worker-pool.sqlite"
    resources_url: str = "http://resources:8080"
    launcher_host: str | None = None
    ext_dir: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        e = os.environ if env is None else env
        return cls(
            host=e.get("AX_HOST", "unknown"),
            db=e.get("AX_WORKER_POOL_DB", cls.db),
            resources_url=e.get("AX_RESOURCES_URL", cls.resources_url),
            launcher_host=e.get("AX_LAUNCHER_HOST") or None,
            ext_dir=e.get("AX_WORKER_POOL_EXT_DIR") or None,
        )


def build(cfg: Config | None = None, *, store: JobsStore | None = None, resources=None, control=None, run_heartbeat_loop: bool = True, launcher=None, dispatch_interval_s: float | None = None, emit=None):
    """(中核の口のアプリ, 作業側の口のアプリ, Service) を返す。"""
    cfg = cfg or Config.from_env()
    store = store or JobsStore(cfg.db)
    sink = None
    if emit is None:
        from axcommon.events import EventSink
        from axcommon.client import TimelineClient
        sink = EventSink(TimelineClient(service_url("timeline")), COMPONENT, cfg.host,
                         os.environ.get("AX_WORKER_POOL_EVENT_OUTBOX", "/data/worker-pool-outbox"))
        emit = sink.emit
    svc = api.PoolService(store, host=cfg.host, resources=resources or ApiClient(cfg.resources_url, token=read_secret_optional("core-token-worker-pool")), emit=emit)

    control = control or ApiClient(service_url("control"))
    from .dispatch import Dispatcher
    dispatcher = Dispatcher(svc, launcher or LauncherClient(host=cfg.launcher_host))
    svc.dispatcher = dispatcher
    interval = dispatch_interval_s if dispatch_interval_s is not None else float(os.environ.get("AX_WORKER_POOL_DISPATCH_INTERVAL_S", "15"))
    if interval <= 0:
        raise ValueError("invalid dispatch interval")
    tasks = []

    async def refresh():
        while True:
            try:
                effective = await asyncio.to_thread(control.get, "/v1/plugins/worker-pool/effective")
                svc.apply_effective(effective)
            except Exception as exc:
                log.warning("control effective unavailable: %s", type(exc).__name__)
            await asyncio.sleep(float(Defaults.load().get("control_poll_s")))

    async def dispatch_loop():
        while True:
            try:
                await asyncio.to_thread(dispatcher.tick)
            except Exception as exc:
                log.warning("dispatch loop: %s", type(exc).__name__)
            await asyncio.sleep(interval)

    async def start_poll():
        tasks.append(asyncio.create_task(refresh()))
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(dispatch_loop()))

    async def stop_poll():
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def health() -> dict:
        return {"jobs": store.list(limit=50) and True}

    core = create_app(
        COMPONENT, VERSION,
        routes=api.build_routes(svc, work_side=False),
        sink=sink,
        extra_health=health,
        on_startup=[start_poll],
        on_shutdown=[stop_poll],
        ext_dir=cfg.ext_dir,
        run_heartbeat_loop=run_heartbeat_loop,
    )

    def work_health() -> dict:
        return {"jobs_open": True}

    work = create_app(
        COMPONENT, VERSION,
        routes=api.build_routes(svc, work_side=True),
        sink=None,
        extra_health=work_health,
        run_heartbeat_loop=False,
    )
    async def authorize(request, call_next):
        if request.method == "GET" and request.url.path == "/healthz":
            return await call_next(request)
        if request.app is core and request.method == "POST" and request.url.path == "/apply":
            # The existing endpoint verifies control-apply-token itself.
            return await call_next(request)
        if request.app is core and request.url.path == "/status":
            expected = read_secret_optional("control-apply-token")
            supplied = parse_bearer(request.headers.get("authorization"))
            if expected and supplied and hmac.compare_digest(expected.encode(), supplied.encode()):
                return await call_next(request)
        state = Path(os.environ.get("AX_STATE_DIR", "/state"))
        is_work = request.app is work
        variable = "AX_PLUGIN_TOKENS" if is_work else "AX_CORE_TOKENS"
        filename = "plugin-tokens.sha256" if is_work else "core-tokens.sha256"
        table = TokenTable.from_file(os.environ.get(variable, str(state / filename)))
        caller = table.identify(parse_bearer(request.headers.get("authorization")))
        allowed = {"change", "release", "work-tdd"} if is_work else {"hq", "manager", "intake", "observer", "control"}
        if caller not in allowed:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        request.state.caller = caller
        return await call_next(request)

    core.add_middleware(BaseHTTPMiddleware, dispatch=authorize)
    work.add_middleware(BaseHTTPMiddleware, dispatch=authorize)
    core.state.service = svc
    work.state.service = svc
    return core, work, svc

"""記憶コネクタの API（D-13、F-18.4）。

中核の口 8080（ax-core）と、作業側の口 8081（ax-work。プラグインのトークンが要る。許可した API だけ）。同じ経路を両方で出す:
  POST /v1/remember                {project, subloop?, host, source, kind, content, supersedes?, valid_from?, refs?}
  POST /v1/search                  {query, limit?, project?, subloop?, host?, kind?, include_history?}
  GET  /v1/memories/{id}           記憶 1 件（memory_get）
  GET  /v1/memories                期間の一覧（memory_list: limit、field、since、until、cursor）
8081 の資格はプラグインのトークンのハッシュの一覧（AX_PLUGIN_TOKENS_FILE。置き場が文書で決まっていないため既定なし。未設定の間は全て 401）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import hmac
import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware

from axcommon import client as axclient, creds, events as axevents
from axcommon.http import create_app, json_error, read_json
from axcommon.redact import Redactor

from . import COMPONENT, VERSION
from .mcp import McpClient
from .service import MemoryService, Result

log = logging.getLogger("memory-connector")


def _resp(r: Result) -> JSONResponse:
    return JSONResponse(r.body, status_code=r.http)


def build_routes(svc: MemoryService, *, work_side: bool, tokens_getter=lambda: None) -> list[Route]:
    def guard(request: Request) -> JSONResponse | None:
        if not work_side:
            return None
        t = tokens_getter()
        if t is None:
            return json_error(401, "unauthorized", "資格の一覧が設定されていない")
        t.reload_if_changed()
        if t.identify(creds.parse_bearer(request.headers.get("authorization"))) is None:
            return json_error(401, "unauthorized", "資格が一覧にない")
        return None

    async def remember(request: Request) -> JSONResponse:
        if (e := guard(request)) is not None:
            return e
        body, err = await read_json(request)
        if err:
            return err
        return _resp(await asyncio.to_thread(svc.remember, body))

    async def search_get(request: Request) -> JSONResponse:
        if (e := guard(request)) is not None:
            return e
        params = dict(request.query_params)
        if "limit" in params:
            try:
                params["limit"] = int(params["limit"])
            except ValueError:
                return json_error(422, "bad_request", "invalid limit")
        if "include_history" in params:
            params["include_history"] = params["include_history"].lower() == "true"
        return _resp(await asyncio.to_thread(svc.search, params))

    async def search(request: Request) -> JSONResponse:
        if (e := guard(request)) is not None:
            return e
        body, err = await read_json(request)
        if err:
            return err
        return _resp(await asyncio.to_thread(svc.search, body))

    async def get_memory(request: Request) -> JSONResponse:
        if (e := guard(request)) is not None:
            return e
        return _resp(await asyncio.to_thread(svc.get, request.path_params["memory_id"]))

    async def list_memories(request: Request) -> JSONResponse:
        if (e := guard(request)) is not None:
            return e
        return _resp(await asyncio.to_thread(svc.list, dict(request.query_params)))

    return [
        Route("/v1/remember", remember, methods=["POST"]),
        Route("/v1/search", search_get, methods=["GET"]),
        Route("/v1/search", search, methods=["POST"]),
        Route("/v1/memories/{memory_id}", get_memory, methods=["GET"]),
        Route("/v1/memories", list_memories, methods=["GET"]),
    ]


def build(environ: dict | None = None, *, mcp: McpClient | None = None, redactor: Redactor | None = None, sink=None,
          tokens: creds.TokenTable | None = None, drain_interval_s: float = 30, run_heartbeat_loop: bool = True):
    """(8080 のアプリ, 8081 のアプリ, MemoryService) を返す。"""
    env = os.environ if environ is None else environ
    host = env.get("AX_HOST", "unknown")
    if mcp is None and env.get("AX_MEMORY_ENDPOINT"):
        mcp = McpClient(env["AX_MEMORY_ENDPOINT"])
    if redactor is None:
        redactor = Redactor(index_path=env.get("AX_REDACT_INDEX", "/redact/index") or None, pii_list_path=env.get("AX_PII_LIST", "/run/secrets/pii-list") or None)
    outbox = axevents.Outbox(env.get("AX_MEMORY_OUTBOX", "/data/outbox"))
    if sink is None:
        sink = axevents.EventSink(axclient.TimelineClient(axclient.service_url("timeline", 8080)), COMPONENT, host, env.get("AX_EVENT_OUTBOX", "/data/event-outbox"))
    if tokens is None and env.get("AX_PLUGIN_TOKENS_FILE"):
        tokens = creds.TokenTable.from_file(env["AX_PLUGIN_TOKENS_FILE"])
    svc = MemoryService(mcp, redactor, sink, outbox)
    task: dict = {}

    async def drain_loop() -> None:
        while True:
            await asyncio.sleep(drain_interval_s)
            try:
                await asyncio.to_thread(svc.drain_outbox)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(sink.flush)
                await asyncio.to_thread(redactor.reload_index_if_changed)
            except Exception:  # noqa: BLE001
                log.exception("outbox の送信に失敗")

    async def startup() -> None:
        task["d"] = asyncio.create_task(drain_loop())

    async def shutdown() -> None:
        t = task.get("d")
        if t:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def health() -> dict:
        return {"memory_endpoint": svc.mcp is not None, "queued": len(outbox), "redact_index": bool(getattr(redactor, "index_ok", True)),
                "counts": dict(svc.counts)}

    core = create_app(COMPONENT, VERSION, routes=build_routes(svc, work_side=False), sink=sink, extra_health=health, on_startup=[startup],
                      on_shutdown=[shutdown], run_heartbeat_loop=run_heartbeat_loop)
    work = create_app(COMPONENT, VERSION, routes=build_routes(svc, work_side=True, tokens_getter=lambda: tokens), sink=None,
                      extra_health=lambda: {"plugin_tokens": tokens is not None}, run_heartbeat_loop=False)
    class ScopedMemoryCredentials(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if not request.url.path.startswith("/v1/"):
                return await call_next(request)
            if tokens is None:
                return json_error(401, "unauthorized", "credential table unavailable")
            tokens.reload_if_changed()
            name = tokens.identify(creds.parse_bearer(request.headers.get("authorization")))
            if name is None:
                return json_error(401, "unauthorized", "credential not found")
            token = creds.parse_bearer(request.headers.get("authorization"))
            digest = creds.sha256_hex(token)
            scopes = {n.split(":", 1)[0] for n, hashed in tokens._entries.items()
                      if n.startswith(("memory.read:", "memory.write:")) and hmac.compare_digest(digest, hashed)}
            if scopes:
                needed = "memory.read" if request.method in ("GET", "HEAD") else "memory.write"
                if needed not in scopes or (request.url.path == "/v1/search" and "memory.read" not in scopes):
                    return json_error(403, "forbidden", f"{needed} required")
            return await call_next(request)

    work.add_middleware(ScopedMemoryCredentials)
    core.state.service = svc
    work.state.service = svc
    return core, work, svc

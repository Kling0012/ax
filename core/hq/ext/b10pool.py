"""Authenticated HQ gateway for the worker-pool's public HTTP interface."""
from __future__ import annotations

import re
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon.client import AuthenticatedPoolClient, ApiError, Unavailable, service_url
from axcommon.creds import read_secret_optional
from core.hq.api import authorize, object_body, InvalidRequest

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_READS = {"jobs": "/v1/jobs", "slots": "/v1/slots", "ledger": "/v1/ledger", "selections": "/v1/selections"}


def register(app):
    if getattr(app.state, "hq_pool", None) is None:
        app.state.hq_pool = AuthenticatedPoolClient(service_url("worker-pool"), token=read_secret_optional("core-token-hq"))

    async def endpoint(request):
        denied = await authorize(request, mutation=request.method == "POST")
        if denied is not None:
            return denied
        name = request.path_params["resource"]
        job_id = request.path_params.get("job_id")
        if name not in _READS or (job_id and (name != "jobs" or not _ID.fullmatch(job_id))):
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        if request.method == "POST" and (name != "jobs" or request.query_params):
            return JSONResponse({"error": {"code": "bad_request"}}, status_code=400)
        try:
            path = _READS[name] + (("/" + job_id) if job_id else "")
            pool = request.app.state.hq_pool
            if request.method == "POST":
                if job_id:
                    body = await object_body(request)
                    if body:
                        raise InvalidRequest("empty_body_required")
                    result = await run_in_threadpool(pool.post, path + "/cancel", {})
                else:
                    body = await object_body(request)
                    result = await run_in_threadpool(pool.post, path, body)
            else:
                allowed = {"state", "role", "limit"} if name == "jobs" and not job_id else ({"job_id"} if name == "selections" else set())
                if set(request.query_params) - allowed or any(len(request.query_params.getlist(k)) != 1 for k in request.query_params):
                    raise InvalidRequest("query_not_allowed")
                result = await run_in_threadpool(pool.get, path, params=dict(request.query_params))
            return JSONResponse(result)
        except InvalidRequest as exc:
            return JSONResponse({"error": {"code": str(exc)}}, status_code=422)
        except Unavailable as exc:
            code = "pool_credential_missing" if str(exc) == "pool_credential_missing" else "pool_unavailable"
            return JSONResponse({"error": {"code": code}}, status_code=503)
        except ApiError as exc:
            return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)
        except Exception:
            return JSONResponse({"error": {"code": "pool_unavailable"}}, status_code=503)

    async def mcp(request):
        denied = await authorize(request, mutation=True)
        if denied is not None:
            return denied
        try:
            data = await object_body(request)
        except InvalidRequest:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        request_id = data.get("id")
        if data.get("jsonrpc") != "2.0" or not isinstance(data.get("params", {}), dict):
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid Request"}})
        method, params = data.get("method"), data.get("params", {})
        names = ("pool_submit", "pool_list", "pool_result", "pool_cancel", "pool_slots", "pool_ledger", "pool_selections")
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "ax-pool", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True}} for name in names]}
        elif method == "tools/call" and params.get("name") in names and isinstance(params.get("arguments", {}), dict):
            import json
            name, arguments = params["name"], params.get("arguments", {})
            try:
                client = request.app.state.hq_pool
                if name == "pool_submit":
                    value = await run_in_threadpool(client.post, "/v1/jobs", arguments)
                elif name in ("pool_result", "pool_cancel"):
                    job_id = arguments.get("job_id")
                    if set(arguments) != {"job_id"} or not isinstance(job_id, str) or not _ID.fullmatch(job_id):
                        raise ValueError("invalid_job_id")
                    path = "/v1/jobs/" + job_id
                    value = await run_in_threadpool(client.post, path + "/cancel", {}) if name == "pool_cancel" else await run_in_threadpool(client.get, path)
                else:
                    path, allowed = {"pool_list": ("/v1/jobs", {"state", "role", "limit"}), "pool_slots": ("/v1/slots", set()),
                                     "pool_ledger": ("/v1/ledger", {"model", "role", "since"}),
                                     "pool_selections": ("/v1/selections", {"job_id"})}[name]
                    if set(arguments) - allowed or any(not isinstance(v, (str, int)) or isinstance(v, bool) for v in arguments.values()):
                        raise ValueError("invalid_arguments")
                    value = await run_in_threadpool(client.get, path, params=arguments)
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
            except Unavailable as exc:
                message = "pool_credential_missing" if str(exc) == "pool_credential_missing" else "pool request failed"
                result = {"content": [{"type": "text", "text": message}], "isError": True}
            except Exception:
                result = {"content": [{"type": "text", "text": "pool request failed"}], "isError": True}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    app.router.routes.extend([
        Route("/v1/pool/mcp", mcp, methods=["POST"]),
        Route("/v1/pool/{resource}", endpoint, methods=["GET", "POST"]),
        Route("/v1/pool/{resource}/{job_id}", endpoint, methods=["GET", "POST"]),
    ])

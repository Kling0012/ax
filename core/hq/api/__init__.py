"""HQ business routes. Mounted by the b10b-1 app through register(app)."""
from __future__ import annotations

import inspect
import json

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon.redact import from_environment
from .hibi import HibiViews
from .service import BusinessAPI, ContractMissing, InvalidRequest, day, identifier

BODY_LIMIT = 131072


def error(status, code, **extra):
    return JSONResponse({"ok": False, "error": {"code": code}, **extra}, status_code=status)


async def authorize(request, *, local=False, mutation=False):
    auth = getattr(request.app.state, "hq_auth", None)
    if auth is None:
        return error(503, "auth_unavailable")
    try:
        who = auth(request)
        if inspect.isawaitable(who):
            who = await who
    except Exception:
        return error(503, "auth_unavailable")
    if not isinstance(who, dict) or not who or who.get("allowed") is False:
        return error(403, "forbidden")
    if local and (who.get("direct_tailnet") is not True or who.get("via") != "tailnet"):
        return error(403, "local_only")
    # Only app configuration selects sample mode. Client flags cannot disable it.
    if mutation and (getattr(request.app.state, "hq_sample", False)
                     or getattr(request.app.state, "sample_mode", False)):
        return error(403, "sample_read_only", preserve_input=True)
    return None


async def object_body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > BODY_LIMIT:
            raise InvalidRequest("body_too_large")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise InvalidRequest("bad_json") from None
    if not isinstance(body, dict):
        raise InvalidRequest("object_required")
    return body


def make_routes(api: BusinessAPI):
    hibi = HibiViews(api)

    def guarded(fn, *, local=False, mutation=False):
        async def endpoint(request):
            denied = await authorize(request, local=local, mutation=mutation)
            if denied is not None:
                return denied
            try:
                return await fn(request)
            except InvalidRequest as exc:
                code = str(exc)
                return error(413 if code == "body_too_large" else 422, code, preserve_input=True)
            except ContractMissing:
                return error(501, "contract_unresolved", confirmed=False, preserve_input=True)
            except Exception:
                return error(503, "component_unavailable", confirmed=False, preserve_input=True)
        return endpoint

    def read_route(path, upstream, *, collection=None, allowed=(), detail=False):
        async def get(request):
            params = dict(request.query_params)
            if set(params) - set(allowed) or any(len(request.query_params.getlist(k)) != 1 for k in params):
                raise InvalidRequest("query_not_allowed")
            target = upstream
            if detail:
                target += "/" + identifier(request.path_params["item_id"])
                # Per-ID reads always retain the source history.
                params["history"] = "1"
            status, result = await run_in_threadpool(api.read, target, params=params, collection=collection)
            return JSONResponse(result, status_code=status)
        return Route(path, guarded(get), methods=["GET"])

    async def request_work(request):
        data = await object_body(request)
        if set(data) != {"op_id", "payload"}:
            raise InvalidRequest("request_fields")
        status, result = await run_in_threadpool(api.submit, "work.request", data["payload"], data["op_id"])
        return JSONResponse(result, status_code=status)

    async def answer(request):
        data = await object_body(request)
        if set(data) - {"op_id", "answer", "quote", "version"} or "op_id" not in data:
            raise InvalidRequest("answer_fields")
        todo_id = request.path_params["todo_id"]
        identifier(todo_id)
        payload = {k: v for k, v in data.items() if k != "op_id"}
        payload["todo_id"] = todo_id
        status, result = await run_in_threadpool(api.submit, "ledger.answer", payload, data["op_id"])
        return JSONResponse(result, status_code=status)

    async def operation(request):
        destination = request.path_params["destination"]
        data = await object_body(request)
        if set(data) != {"op_id", "payload"}:
            raise InvalidRequest("request_fields")
        status, result = await run_in_threadpool(api.submit, destination, data["payload"], data["op_id"])
        return JSONResponse(result, status_code=status)

    async def unfinished(request):
        data = await object_body(request)
        if set(data) != {"items"} or not isinstance(data["items"], list) or len(data["items"]) > 100:
            raise InvalidRequest("items_required")
        return JSONResponse(await run_in_threadpool(api.unfinished, data["items"]))

    async def digest(request):
        target = day(request.path_params["date"])
        status, result = await run_in_threadpool(api.digest, target, getattr(request.app.state, "notify", None))
        return JSONResponse(result, status_code=status)

    async def focus(request):
        status, result = await run_in_threadpool(api.read, "/v1/focus-state")
        return JSONResponse(result, status_code=status)

    async def daily(request):
        target = day(request.path_params["date"])
        status, result = await run_in_threadpool(api.read, f"/v1/daily-state/{target}")
        return JSONResponse(result, status_code=status)

    async def hibi_now(request):
        if request.query_params:
            raise InvalidRequest("query_not_allowed")
        return JSONResponse(await run_in_threadpool(hibi.now))

    async def hibi_day(request):
        if set(request.query_params) != {"date"} or len(request.query_params.getlist("date")) != 1:
            raise InvalidRequest("date_required")
        target = day(request.query_params["date"])
        return JSONResponse(await run_in_threadpool(hibi.daily, target))

    # Starlette normally treats HEAD as GET. These two paths explicitly allow GET only.
    async def hibi_only_get(request, fn):
        if request.method != "GET":
            return JSONResponse({"ok": False, "error": {"code": "method_not_allowed"}}, status_code=405,
                                headers={"Allow": "GET"})
        return await guarded(fn, local=True)(request)

    async def now_endpoint(request):
        return await hibi_only_get(request, hibi_now)

    async def day_endpoint(request):
        return await hibi_only_get(request, hibi_day)

    routes = [
        Route("/v1/requests", guarded(request_work, mutation=True), methods=["POST"]),
        Route("/v1/todos/{todo_id}/answer", guarded(answer, mutation=True), methods=["POST"]),
        Route("/v1/operations/{destination}", guarded(operation, mutation=True), methods=["POST"]),
        Route("/v1/reports/unfinished/check", guarded(unfinished), methods=["POST"]),
        Route("/v1/digests/{date}", guarded(digest), methods=["GET"]),
        Route("/v1/focus-state", guarded(focus), methods=["GET"]),
        Route("/v1/daily-state/{date}", guarded(daily), methods=["GET"]),
        Route("/v1/hibi/now", now_endpoint, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT"]),
        Route("/v1/hibi/day", day_endpoint, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT"]),
        read_route("/v1/todos", "/v1/todos", collection="todos", allowed=("state", "project", "subloop", "owner", "due")),
        read_route("/v1/todos/{item_id}", "/v1/todos", detail=True),
    ]
    for name, allowed in (("goals", ("state",)), ("promises", ("todo_id", "state", "due", "due_by")),
                          ("schedules", ("state", "from", "to"))):
        routes.append(read_route(f"/v1/{name}", f"/v1/{name}", collection=name, allowed=allowed))
        routes.append(read_route(f"/v1/{name}/{{item_id}}", f"/v1/{name}", detail=True))
    return routes


def register(app):
    """No service startup, timers, or state creation. The HQ app owns authentication."""
    api = getattr(app.state, "hq_api", None)
    if api is None:
        redactor = getattr(app.state, "redactor", None)
        if redactor is None:
            try:
                import os
                if os.environ.get("AX_REDACT_INDEX") and os.environ.get("AX_PII_LIST"):
                    redactor = from_environment()
            except Exception:
                redactor = None
        api = BusinessAPI.from_environment(redactor=redactor)
        app.state.hq_api = api
    app.router.routes.extend(make_routes(api))

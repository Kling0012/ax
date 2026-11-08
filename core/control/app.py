"""制御 API の口（S-5.7、F-18.4）。中核の口（8080）と作業側の口（8081）の2つのアプリ。

作業側の口が通す物（F-18.4 の表。これ以外は 404）:
  GET /v1/plugins、GET /v1/plugins/{id}、GET /v1/plugins/{id}/effective   読み取りだけ

書き込み系（POST。/v1/validate と /v1/permissions/check を除く）は呼び出し元の資格を要する（S-5.7）。
`issued_by` は資格から決め、本文の値は照合だけに使う（S-5.3）。
"""
from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import schemas
from axcommon.creds import TokenTable, parse_bearer
from axcommon.http import create_app, json_error

from . import VERSION, Reject
from . import callers
from .service import ControlService

MAX_BODY_BYTES = 1024 * 1024


def _reject_response(e: Reject) -> JSONResponse:
    return json_error(e.status, e.code, e.message)


async def _json(request: Request, schema: str | None = None, fragment: str | None = None):
    raw = await request.body()
    if len(raw or b"") > MAX_BODY_BYTES:
        return None, json_error(413, "too_large", "本文が大きすぎる")
    import json as _json_mod

    try:
        data = _json_mod.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return None, json_error(400, "bad_json", "本文が JSON でない")
    if not isinstance(data, dict):
        return None, json_error(422, "bad_request", "本文は object が必須")
    if schema:
        errs = schemas.errors(schema, data, fragment)
        if errs:
            return None, json_error(422, "bad_request", "; ".join(errs[:5]))
    return data, None


def _issuer(request: Request) -> tuple[str | None, str]:
    header = request.headers.get("Authorization")
    declared = None
    if request.method in ("POST", "PUT", "PATCH"):
        # issued_by は資格から決める。本文の値は照合だけに使う（S-5.3）
        declared = getattr(request.state, "declared_issued_by", None)
    return callers.issuer_for(header, declared=declared, core_tokens=getattr(request.app.state, "core_tokens", None))


def _require_issuer(request: Request, allowed: set[str] | None = None) -> tuple[str | None, JSONResponse | None]:
    issued_by, basis = _issuer(request)
    if issued_by is None:
        request.app.state.control._rejected("-", {}, f"unauthorized: {basis}")
        return None, json_error(401, "unauthorized", f"呼び出し元の資格が決まらない（{basis}）")
    if allowed is not None and issued_by not in allowed:
        return None, json_error(403, "forbidden", f"issued_by={issued_by} はこの操作をできない")
    declared = getattr(request.state, "declared_issued_by", None)
    if declared is not None and declared != issued_by:
        request.app.state.control._rejected("-", {"issued_by": declared}, "issuer_mismatch")
        return None, json_error(400, "issuer_mismatch", "本文の issued_by が呼び出し元の資格と違う")
    return issued_by, None


def _plugin_caller(request: Request) -> str | None:
    table: TokenTable = request.app.state.plugin_tokens
    table.reload_if_changed()
    return table.identify(parse_bearer(request.headers.get("Authorization")))


# ---------- 中核の口（8080）----------


async def plugins_list(request: Request):
    service: ControlService = request.app.state.control
    out = []
    for p in service.store.list_plugins():
        view = service.plugin_view(p["id"])
        out.append({k: v for k, v in view.items() if k != "manifest"})
    return JSONResponse({"plugins": out})


async def plugin_get(request: Request):
    view = request.app.state.control.plugin_view(request.path_params["plugin_id"])
    if view is None:
        return json_error(404, "not_found", "プラグインが登録に無い")
    return JSONResponse(view)


async def plugin_effective(request: Request):
    try:
        return JSONResponse(request.app.state.control.effective(request.path_params["plugin_id"]))
    except Reject as e:
        return _reject_response(e)


async def commands_post(request: Request):
    data, err = await _json(request)
    if err:
        return err
    request.state.declared_issued_by = (data or {}).get("issued_by") if isinstance(data, dict) else None
    issued_by, resp = _require_issuer(request)
    if resp:
        return resp
    try:
        command = request.app.state.control.issue(data, issued_by=issued_by)
    except Reject as e:
        return _reject_response(e)
    return JSONResponse(command, status_code=201)


async def commands_list(request: Request):
    q = request.query_params
    state = q.get("state")
    if state and state not in ("active", "expired_pending", "released"):
        return json_error(422, "bad_request", "state は active|expired_pending|released")
    return JSONResponse({"commands": request.app.state.control.list_commands(state=state, issued_by=q.get("issued_by"))})


async def command_release(request: Request):
    data, err = await _json(request)
    if err:
        return err
    request.state.declared_issued_by = (data or {}).get("issued_by") if isinstance(data, dict) else None
    issued_by, resp = _require_issuer(request)
    if resp:
        return resp
    try:
        command = request.app.state.control.release(request.path_params["command_id"], by=issued_by,
                                                    reason=(data or {}).get("reason", ""))
    except Reject as e:
        return _reject_response(e)
    return JSONResponse(command)


async def command_renew(request: Request):
    data, err = await _json(request)
    if err:
        return err
    request.state.declared_issued_by = (data or {}).get("issued_by") if isinstance(data, dict) else None
    issued_by, resp = _require_issuer(request)
    if resp:
        return resp
    try:
        command = request.app.state.control.renew(request.path_params["command_id"], data or {}, by=issued_by)
    except Reject as e:
        return _reject_response(e)
    return JSONResponse(command)


async def permissions_list(request: Request):
    return JSONResponse({"permissions": request.app.state.control.list_permissions()})


async def permissions_check(request: Request):
    data, err = await _json(request, "permission-check", "/$defs/request")
    if err:
        return err
    try:
        return JSONResponse(request.app.state.control.check_permission(data))
    except Reject as e:
        return _reject_response(e)


async def permissions_post(request: Request):
    data, err = await _json(request)
    if err:
        return err
    request.state.declared_issued_by = (data or {}).get("issued_by") if isinstance(data, dict) else None
    issued_by, resp = _require_issuer(request, {"user"})
    if resp:
        return resp
    try:
        return JSONResponse(request.app.state.control.create_permission(data, by=issued_by), status_code=201)
    except Reject as e:
        return _reject_response(e)


async def permission_disable(request: Request):
    data, err = await _json(request)
    if err:
        return err
    request.state.declared_issued_by = (data or {}).get("issued_by") if isinstance(data, dict) else None
    issued_by, resp = _require_issuer(request, {"user"})
    if resp:
        return resp
    try:
        return JSONResponse(request.app.state.control.disable_permission(request.path_params["permission_id"], by=issued_by))
    except Reject as e:
        return _reject_response(e)


async def plugin_items(request: Request):
    """項目をサブループへ渡す中継。呼べるのは入口（intake）の資格だけ（S-5.7）。"""
    data, err = await _json(request)
    if err:
        return err
    request.app.state.core_tokens.reload_if_changed()
    caller = request.app.state.core_tokens.identify(parse_bearer(request.headers.get("Authorization")))
    if caller != "intake":
        return json_error(403, "forbidden", "この中継は入口（intake）の資格だけ（S-5.7）")
    try:
        status, body = request.app.state.control.relay_items(request.path_params["plugin_id"], data)
        return JSONResponse(body if body is not None else {}, status_code=status)
    except Reject as e:
        return _reject_response(e)


async def validate_post(request: Request):
    data, err = await _json(request, "validate", "/$defs/request")
    if err:
        return err
    try:
        return JSONResponse(request.app.state.control.validate(data["kind"], data["body"]))
    except Reject as e:
        return _reject_response(e)


async def timers_post(request: Request):
    data, err = await _json(request)
    if err:
        return err
    issued_by, resp = _require_issuer(request, {"manager"})
    if resp:
        return resp
    try:
        return JSONResponse(request.app.state.control.create_timer(data, issued_by=issued_by), status_code=201)
    except Reject as e:
        return _reject_response(e)


async def timers_list(request: Request):
    state = request.query_params.get("state")
    return JSONResponse({"timers": request.app.state.control.list_timers(state)})


async def timer_get(request: Request):
    t = request.app.state.control.store.get_timer(request.path_params["timer_id"])
    if t is None:
        return json_error(404, "not_found", "タイマーが無い")
    return JSONResponse(t)


async def timer_fire(request: Request):
    return json_error(501, "timer_transport_undefined", "S-7.8/F-18.4: timer-runner の発火報告の到達経路と資格が未定義")


async def sweep(request: Request):
    """期限の走査を1回だけ手で回す（試験と点検用。常時は10秒ごと。D-4.3）。"""
    _, resp = _require_issuer(request)
    if resp:
        return resp
    service = request.app.state.control
    commands = service.sweep_expiry()
    timers = service.sweep_timers()
    service.retry_limits_registration()
    service.sync_limits()
    return JSONResponse({"commands": commands, "timers": timers})


# ---------- 作業側の口（8081。F-18.4 の表）----------


def _work_plugin_guard(request: Request) -> JSONResponse | None:
    if _plugin_caller(request) is None:
        return json_error(401, "unauthorized", "トークンが無いか一覧に無い（F-18.4）")
    return None


async def work_plugins_list(request: Request):
    guard = _work_plugin_guard(request)
    if guard:
        return guard
    service: ControlService = request.app.state.control
    out = [{k: v for k, v in service.plugin_view(p["id"]).items() if k != "manifest"}
           for p in service.store.list_plugins()]
    return JSONResponse({"plugins": out})


async def work_plugin_get(request: Request):
    guard = _work_plugin_guard(request)
    if guard:
        return guard
    view = request.app.state.control.plugin_view(request.path_params["plugin_id"])
    if view is None:
        return json_error(404, "not_found", "プラグインが登録に無い")
    return JSONResponse(view)


async def work_plugin_effective(request: Request):
    guard = _work_plugin_guard(request)
    if guard:
        return guard
    try:
        return JSONResponse(request.app.state.control.effective(request.path_params["plugin_id"]))
    except Reject as e:
        return _reject_response(e)


async def work_delivery_permission(request: Request):
    plugin_id = _plugin_caller(request)
    if not plugin_id:
        return json_error(401, "unauthorized", "credential required")
    params = request.query_params
    if set(params.keys()) != {"project", "repo", "destination", "operation"} or any(
            len(params.getlist(k)) != 1 or not params.get(k) for k in params.keys()):
        return json_error(422, "bad_request", "invalid permission scope")
    checker = getattr(request.app.state.control, "read_delivery_permission", None)
    target = {k: params[k] for k in ("project", "repo", "destination")}
    try:
        allowed = bool(checker and checker(plugin_id, target, params["operation"]) is True)
    except Exception:
        allowed = False
    return JSONResponse({"allowed": allowed})


async def work_not_found(request: Request):
    return json_error(404, "not_found", "作業側の口にこの API は無い（F-18.4 の表）")


def create_core_app(service: ControlService, *, ext_dir: str | None = None, sink=None):
    routes = [
        Route("/v1/plugins", plugins_list, methods=["GET"]),
        Route("/v1/plugins/{plugin_id}", plugin_get, methods=["GET"]),
        Route("/v1/plugins/{plugin_id}/effective", plugin_effective, methods=["GET"]),
        Route("/v1/commands", commands_post, methods=["POST"]),
        Route("/v1/commands", commands_list, methods=["GET"]),
        Route("/v1/commands/{command_id}/release", command_release, methods=["POST"]),
        Route("/v1/commands/{command_id}/renew", command_renew, methods=["POST"]),
        Route("/v1/permissions", permissions_list, methods=["GET"]),
        Route("/v1/permissions", permissions_post, methods=["POST"]),
        Route("/v1/permissions/check", permissions_check, methods=["POST"]),
        Route("/v1/permissions/{permission_id}/disable", permission_disable, methods=["POST"]),
        Route("/v1/plugins/{plugin_id}/items", plugin_items, methods=["POST"]),
        Route("/v1/validate", validate_post, methods=["POST"]),
        Route("/v1/timers", timers_post, methods=["POST"]),
        Route("/v1/timers", timers_list, methods=["GET"]),
        Route("/v1/timers/{timer_id}", timer_get, methods=["GET"]),
        Route("/v1/timers/{timer_id}/fire", timer_fire, methods=["POST"]),
        Route("/v1/sweep", sweep, methods=["POST"]),
    ]

    def extra_health():
        from . import graphcheck

        return {
            "plugins": len(service.store.list_plugins()),
            "effective_limits": service.store.limits_sync_status(),
            "graph_check": "tools/ax_graph_check.py" if graphcheck.available() else "unavailable（B7 待ち）",
        }

    app = create_app("control", VERSION, routes=routes, sink=sink if sink is not None else service.sink,
                     extra_health=extra_health, ext_dir=ext_dir or _default_ext_dir("control"),
                     on_startup=[service.register], run_heartbeat_loop=True)
    app.state.control = service
    app.state.plugin_tokens = _plugin_table()
    app.state.core_tokens = callers.core_table()
    app.state.mouth = "core"

    async def on_reject(request: Request, exc: Reject):
        return _reject_response(exc)

    app.add_exception_handler(Reject, on_reject)
    return app


def create_work_app(service: ControlService, *, ext_dir: str | None = None, sink=None):
    routes = [
        Route("/v1/permissions/check", work_delivery_permission, methods=["GET"]),
        Route("/v1/plugins", work_plugins_list, methods=["GET"]),
        Route("/v1/plugins/{plugin_id}", work_plugin_get, methods=["GET"]),
        Route("/v1/plugins/{plugin_id}/effective", work_plugin_effective, methods=["GET"]),
        Route("/{rest_of_path:path}", work_not_found, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    ]
    app = create_app("control-work", VERSION, routes=routes, sink=None,
                     extra_health=lambda: {"mouth": "work"}, run_heartbeat_loop=False)
    app.state.control = service
    app.state.plugin_tokens = _plugin_table()
    app.state.core_tokens = callers.core_table()
    app.state.mouth = "work"

    async def on_reject(request: Request, exc: Reject):
        return _reject_response(exc)

    app.add_exception_handler(Reject, on_reject)
    return app


def _plugin_table() -> TokenTable:
    from core.ledger import workauth

    return workauth.plugin_table()


def _default_ext_dir(component: str) -> str | None:
    base = os.environ.get("AX_EXT_DIR")
    if base:
        return os.path.join(base, component)
    return None

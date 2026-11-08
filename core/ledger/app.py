"""台帳の口（S-9.3、S-9.5、F-18.4）。中核の口（8080）と作業側の口（8081）の2つのアプリ。

作業側の口が通す物（F-18.4 の表。これ以外は 404）:
  POST /v1/todos、GET /v1/todos、GET /v1/todos/{id}   作成者はプラグイン ID に固定
  PUT/GET /v1/focus-state、PUT/GET /v1/daily-state/{date}   hibi-state だけ（ほかは403、無効は401）
"""
from __future__ import annotations

import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon.creds import parse_bearer
from axcommon.http import create_app, json_error
from axcommon import schemas

from . import HIBI_STATE_PLUGIN, STATE_CORE_CALLERS, VERSION, Reject
from .service import LedgerService
from . import workauth

MAX_BODY_BYTES = 512 * 1024  # 読み取りの上限（413 の判定は S-17 hibi_body_max_kib で別に行う）


def _reject_response(e: Reject) -> JSONResponse:
    return json_error(e.status, e.code, e.message)


async def _json_body(request: Request, schema: str | None = None) -> tuple[Any, JSONResponse | None, int]:
    raw = await request.body()
    size = len(raw or b"")
    if size > MAX_BODY_BYTES:
        return None, json_error(413, "too_large", "本文が大きすぎる"), size
    import json as _json

    try:
        data = _json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return None, json_error(400, "bad_json", "本文が JSON でない"), size
    if not isinstance(data, dict):
        return None, json_error(422, "schema", "本文が JSON の物でない"), size
    if schema:
        # focus/daily は種類ごとの錯誤コードを出すため、ここでは検査しない（service で行う）
        if schema not in ("focus-state", "daily-state"):
            errs = schemas.errors(schema, data)
            if errs:
                return None, json_error(422, "bad_request", "; ".join(errs[:5])), size
    return data, None, size


def _caller_core(request: Request) -> str | None:
    """中核の口の書き込みの資格。core-tokens の表の名前（intake、hq、manager など）。"""
    table = request.app.state.core_tokens
    workauth.reload_if_changed(table)
    return table.identify(parse_bearer(request.headers.get("Authorization")))


def _via(request: Request) -> str | None:
    """入口が確かめた受け口（hq / local / ssh / tailnet）。入口の保証がある値だけを信用する（S-3.2）。"""
    caller = _caller_core(request)
    if caller == "hq":
        return "hq"
    if caller == "intake":
        return request.headers.get("X-AX-Via")
    return None


def _plugin_caller(request: Request) -> str | None:
    table = request.app.state.plugin_tokens
    workauth.reload_if_changed(table)
    return table.identify(parse_bearer(request.headers.get("Authorization")))


# ---------- 中核の口（8080）----------


async def todos_create(request: Request):
    caller = _caller_core(request)
    if caller is None:
        return json_error(401, "unauthorized", "書き込みには中核の資格が要る（core-tokens）")
    data, err, _ = await _json_body(request, "todo")
    if err:
        return err
    todo = request.app.state.ledger.create_todo(data, actor=caller, via=_via(request))
    return JSONResponse(todo, status_code=201)


async def todos_list(request: Request):
    q = request.query_params
    todos = request.app.state.ledger.list_todos(
        state=q.get("state"), project=q.get("project"), subloop=q.get("subloop"),
        due=q.get("due") in ("1", "true"), owner=q.get("owner"))
    return JSONResponse({"todos": todos})


async def todo_get(request: Request):
    todo = request.app.state.ledger.get_todo(request.path_params["todo_id"],
                                             with_history=request.query_params.get("history") in ("1", "true"))
    if todo is None:
        return json_error(404, "not_found", "ToDo が無い")
    return JSONResponse(todo)


async def todo_work_request(request: Request):
    if _caller_core(request) != "intake":
        return json_error(403, "unauthorized", "intake credential required")
    data, err, _ = await _json_body(request)
    if err:
        return err
    result = request.app.state.ledger.record_work_request(
        request.path_params["todo_id"], data, actor="intake", via=_via(request))
    return JSONResponse(result)


async def todo_transition(request: Request):
    caller = _caller_core(request)
    if caller is None:
        return json_error(401, "unauthorized", "書き込みには中核の資格が要る（core-tokens）")
    data, err, _ = await _json_body(request, "todo-transition")
    if err:
        return err
    todo = request.app.state.ledger.transition(request.path_params["todo_id"], data, actor=caller, via=_via(request))
    return JSONResponse(todo)


async def todo_answer(request: Request):
    caller = _caller_core(request)
    if caller is None:
        return json_error(401, "unauthorized", "書き込みには中核の資格が要る（core-tokens）")
    data, err, _ = await _json_body(request, "todo-answer")
    if err:
        return err
    todo = request.app.state.ledger.answer(request.path_params["todo_id"], data, actor=caller, via=_via(request))
    return JSONResponse(todo)


async def focus_get_core(request: Request):
    caller = _caller_core(request)
    if caller not in STATE_CORE_CALLERS:
        return json_error(401, "unauthorized", "集中状態は HQ と許可された呼び出し元だけ（S-9.5）")
    return JSONResponse(request.app.state.ledger.get_focus())


async def focus_put_core(request: Request):
    caller = _caller_core(request)
    if caller not in STATE_CORE_CALLERS | {"intake"}:
        return json_error(401, "unauthorized", "集中状態は HQ と許可された呼び出し元だけ（S-9.5）")
    data, err, size = await _json_body(request, "focus-state")
    if err:
        return err
    out = request.app.state.ledger.put_focus(data, issuer=caller, body_bytes=size)
    return JSONResponse(out)


async def daily_get_core(request: Request):
    caller = _caller_core(request)
    if caller not in STATE_CORE_CALLERS:
        return json_error(401, "unauthorized", "日次状態は HQ と許可された呼び出し元だけ（S-9.5）")
    return JSONResponse(request.app.state.ledger.get_daily(request.path_params["date"]))


async def daily_put_core(request: Request):
    caller = _caller_core(request)
    if caller not in STATE_CORE_CALLERS | {"intake"}:
        return json_error(401, "unauthorized", "日次状態は HQ と許可された呼び出し元だけ（S-9.5）")
    data, err, size = await _json_body(request, "daily-state")
    if err:
        return err
    out = request.app.state.ledger.put_daily(request.path_params["date"], data, issuer=caller, body_bytes=size)
    return JSONResponse(out)


def _item_routes() -> list[Route]:
    routes: list[Route] = []
    for kind in ("goal", "promise", "schedule"):
        routes += _sync_make(kind)
    return routes


def _sync_make(kind: str) -> list[Route]:
    async def create(request: Request):
        caller = _caller_core(request)
        if caller is None:
            return json_error(401, "unauthorized", "書き込みには中核の資格が要る（core-tokens）")
        data, err, _ = await _json_body(request)
        if err:
            return err
        item = request.app.state.ledger.create_item(kind, data, actor=caller)
        return JSONResponse(item, status_code=201)

    async def list_(request: Request):
        items = request.app.state.ledger.list_items(kind, dict(request.query_params))
        return JSONResponse({kind + "s": items})

    async def get(request: Request):
        item = request.app.state.ledger.get_item(kind, request.path_params["item_id"],
                                                 with_history=request.query_params.get("history") in ("1", "true"))
        if item is None:
            return json_error(404, "not_found", f"{kind} が無い")
        return JSONResponse(item)

    async def patch(request: Request):
        caller = _caller_core(request)
        if caller is None:
            return json_error(401, "unauthorized", "書き込みには中核の資格が要る（core-tokens）")
        data, err, _ = await _json_body(request)
        if err:
            return err
        return JSONResponse(request.app.state.ledger.patch_item(kind, request.path_params["item_id"], data, actor=caller))

    async def transition(request: Request):
        caller = _caller_core(request)
        if caller is None:
            return json_error(401, "unauthorized", "書き込みには中核の資格が要る（core-tokens）")
        data, err, _ = await _json_body(request)
        if err:
            return err
        return JSONResponse(request.app.state.ledger.transition_item(kind, request.path_params["item_id"], data, actor=caller))

    return [Route(f"/v1/{kind}s", create, methods=["POST"]),
            Route(f"/v1/{kind}s", list_, methods=["GET"]),
            Route(f"/v1/{kind}s/{{item_id}}", get, methods=["GET"]),
            Route(f"/v1/{kind}s/{{item_id}}", patch, methods=["PATCH"]),
            Route(f"/v1/{kind}s/{{item_id}}/transition", transition, methods=["POST"])]


async def decisions_create(request: Request):
    caller = _caller_core(request)
    if caller is None:
        return json_error(401, "unauthorized", "判断の記録は入口の資格だけ（S-9.6、D-8）")
    data, err, _ = await _json_body(request)
    if err:
        return err
    d = request.app.state.ledger.record_decision(data, actor=caller, via=_via(request))
    return JSONResponse(d, status_code=201)


async def decisions_list(request: Request):
    q = dict(request.query_params)
    return JSONResponse({"decisions": request.app.state.ledger.list_decisions(q)})


async def decision_get(request: Request):
    d = request.app.state.ledger.get_decision(request.path_params["decision_id"])
    if d is None:
        return json_error(404, "not_found", "判断の記録が無い")
    return JSONResponse(d)


async def state_history(request: Request):
    if _caller_core(request) not in STATE_CORE_CALLERS:
        return json_error(401, "unauthorized", "状態の履歴には中核の資格が要る")
    kind = request.path_params["kind"]
    if kind not in ("focus", "daily"):
        return json_error(404, "not_found", "履歴は focus か daily")
    date = request.query_params.get("date")
    return JSONResponse({"history": request.app.state.ledger.state_history(kind, date)})


def create_core_app(service: LedgerService, *, ext_dir: str | None = None, sink=None) -> "Starlette":  # noqa: F821
    from starlette.applications import Starlette

    routes = [
        Route("/v1/todos", todos_create, methods=["POST"]),
        Route("/v1/todos", todos_list, methods=["GET"]),
        Route("/v1/todos/{todo_id}", todo_get, methods=["GET"]),
        Route("/v1/todos/{todo_id}/work-requests", todo_work_request, methods=["POST"]),
        Route("/v1/todos/{todo_id}/transition", todo_transition, methods=["POST"]),
        Route("/v1/todos/{todo_id}/answer", todo_answer, methods=["POST"]),
        Route("/v1/focus-state", focus_get_core, methods=["GET"]),
        Route("/v1/focus-state", focus_put_core, methods=["PUT"]),
        Route("/v1/daily-state/{date}", daily_get_core, methods=["GET"]),
        Route("/v1/daily-state/{date}", daily_put_core, methods=["PUT"]),
        Route("/v1/state-history/{kind}", state_history, methods=["GET"]),
        Route("/v1/decisions", decisions_create, methods=["POST"]),
        Route("/v1/decisions", decisions_list, methods=["GET"]),
        Route("/v1/decisions/{decision_id}", decision_get, methods=["GET"]),
    ] + _item_routes()

    def extra_health():
        return {
            "todos": len(service.list_todos()),
            "secret_scan": "axcommon.redact" if service.secret_scan_available else "unavailable（B1 待ち）",
            "cursor_item_state": service.store.get_cursor("item.state"),
        }

    app = create_app("ledger", VERSION, routes=routes, sink=sink if sink is not None else service.sink,
                     extra_health=extra_health, ext_dir=ext_dir or _default_ext_dir("ledger"),
                     run_heartbeat_loop=True)
    app.state.ledger = service
    app.state.plugin_tokens = workauth.plugin_table()
    app.state.core_tokens = workauth.core_table()
    app.state.mouth = "core"

    async def on_reject(request: Request, exc: Reject):
        return _reject_response(exc)

    app.add_exception_handler(Reject, on_reject)
    return app


# ---------- 作業側の口（8081。F-18.4 の表）----------


def _hibi_guard(request: Request) -> str | None | JSONResponse:
    """集中状態・日次状態の口の呼び出し元検査。JSONResponse を返したらそのまま応じる。"""
    caller = _plugin_caller(request)
    if caller is None:
        return json_error(401, "unauthorized", "トークンが無いか一覧に無い（F-18.4）")
    if caller != HIBI_STATE_PLUGIN:
        return json_error(403, "plugin_not_allowed", f"この口は {HIBI_STATE_PLUGIN} だけ（S-9.5）")
    return None


async def work_todos_create(request: Request):
    caller = _plugin_caller(request)
    if caller is None:
        return json_error(401, "unauthorized", "トークンが無いか一覧に無い（F-18.4）")
    data, err, _ = await _json_body(request, "todo")
    if err:
        return err
    # 作成者はプラグイン ID に固定する（F-18.4）。本文の値は信用しない。
    todo = request.app.state.ledger.create_todo(data, actor=f"plugin:{caller}")
    return JSONResponse(todo, status_code=201)


async def work_todos_list(request: Request):
    if _plugin_caller(request) is None:
        return json_error(401, "unauthorized", "トークンが無いか一覧に無い（F-18.4）")
    q = request.query_params
    todos = request.app.state.ledger.list_todos(state=q.get("state"), project=q.get("project"),
                                                subloop=q.get("subloop"), due=q.get("due") in ("1", "true"))
    return JSONResponse({"todos": todos})


async def work_todo_get(request: Request):
    if _plugin_caller(request) is None:
        return json_error(401, "unauthorized", "トークンが無いか一覧に無い（F-18.4）")
    todo = request.app.state.ledger.get_todo(request.path_params["todo_id"])
    if todo is None:
        return json_error(404, "not_found", "ToDo が無い")
    return JSONResponse(todo)


async def work_focus_put(request: Request):
    guard = _hibi_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    data, err, size = await _json_body(request, "focus-state")
    if err:
        return err
    out = request.app.state.ledger.put_focus(data, issuer=f"plugin:{HIBI_STATE_PLUGIN}", body_bytes=size)
    return JSONResponse(out)


async def work_focus_get(request: Request):
    guard = _hibi_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    return JSONResponse(request.app.state.ledger.get_focus())


async def work_daily_put(request: Request):
    guard = _hibi_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    data, err, size = await _json_body(request, "daily-state")
    if err:
        return err
    out = request.app.state.ledger.put_daily(request.path_params["date"], data,
                                             issuer=f"plugin:{HIBI_STATE_PLUGIN}", body_bytes=size)
    return JSONResponse(out)


async def work_daily_get(request: Request):
    guard = _hibi_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    return JSONResponse(request.app.state.ledger.get_daily(request.path_params["date"]))


async def work_not_found(request: Request):
    """作業側の口に載せない物は全部 404（遷移・回答・目標・約束・予定・判断の記録。F-18.4）。"""
    return json_error(404, "not_found", "作業側の口にこの API は無い（F-18.4 の表）")


def create_work_app(service: LedgerService, *, ext_dir: str | None = None, sink=None) -> "Starlette":  # noqa: F821
    from starlette.applications import Starlette

    routes = [
        Route("/v1/todos", work_todos_create, methods=["POST"]),
        Route("/v1/todos", work_todos_list, methods=["GET"]),
        Route("/v1/todos/{todo_id}", work_todo_get, methods=["GET"]),
        Route("/v1/focus-state", work_focus_put, methods=["PUT"]),
        Route("/v1/focus-state", work_focus_get, methods=["GET"]),
        Route("/v1/daily-state/{date}", work_daily_put, methods=["PUT"]),
        Route("/v1/daily-state/{date}", work_daily_get, methods=["GET"]),
        Route("/{rest_of_path:path}", work_not_found, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    ]
    app = create_app("ledger-work", VERSION, routes=routes, sink=None,
                     extra_health=lambda: {"mouth": "work"}, run_heartbeat_loop=False)
    app.state.ledger = service
    app.state.plugin_tokens = workauth.plugin_table()
    app.state.core_tokens = workauth.core_table()
    app.state.mouth = "work"

    async def on_reject(request: Request, exc: Reject):
        return _reject_response(exc)

    app.add_exception_handler(Reject, on_reject)
    return app


def _default_ext_dir(component: str) -> str | None:
    base = os.environ.get("AX_EXT_DIR")
    if base:
        return os.path.join(base, component)
    return None

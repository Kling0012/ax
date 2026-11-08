"""b10a の HQ への差し込み（axcommon.ext の register(app)）。

足す物:
  GET  /v1/notify/status          通知配送の状態（件数、再通知の時間、朝の要約の時刻、検査の可否）
  GET  /v1/notify/open/{todo_id}  通知の参照から同じ ToDo を開く（S-22.4）。質問・選択肢・操作の候補を返す
  POST /v1/notify/actions         カードからの完了・保留 {todo_id, action: complete|park, op_id, reason?, check_at?}
  POST /v1/notify/test            テスト通知（S-24 条件 4）。1 回だけ。受付（HTTP 200 と ID）だけを返す
  GET  /v1/emergency/scopes       緊急ボタンの範囲の候補（all、commanders、プラグイン）
  POST /v1/emergency              緊急ボタン {scope, confirm: true, op_id}。入口 `emergency`（from=hq）へ出す
認証: HQ の認証（S-22.7。b10b が作る）が `app.state.hq_auth(request)` として渡す。許可された利用者なら本人の記述（dict）、
そうでなければ None を返す関数（同期でも非同期でもよい）。無い間は全部 503 で拒否する（閉じる側）。OPEN.md の b10a。
画面（HTML）はここに無い。画面の側は b10b。ここは画面が呼ぶ API だけ。
周期の処理（質問の走査、再配送、朝の要約）は、環境変数 AX_HQ_NOTIFY_RUN が 0 でなければ HQ の startup/shutdown に接続する。
"""
from __future__ import annotations

import inspect
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import schemas
from axcommon.client import ApiClient, ApiError, Unavailable, service_url
from axcommon.http import json_error
from core.hq.api import object_body, error
from core.hq.api.service import InvalidRequest
from core.hq.notify.receipt_auth import authorize_component

from core.hq.notify.intake import IntakeClient, IntakeError
from core.hq.notify.loop import NotifyHub, Runner, build_hub

log = logging.getLogger("hq.ext.b10a")

SCOPE_FIXED = ("all", "commanders")


def _redactor_from_env():
    if not os.environ.get("AX_REDACT_INDEX") or not os.environ.get("AX_PII_LIST"):
        return None
    try:
        from axcommon.redact import from_environment

        return from_environment()
    except Exception:  # noqa: BLE001  検査器が組めなくても HQ は動く（通知は検査不能として止まる）
        log.error("秘密検査を組めなかった")
        return None


async def _authorize(request: Request) -> tuple[dict | None, JSONResponse | None]:
    fn = getattr(request.app.state, "hq_auth", None)
    if fn is None:
        return None, json_error(503, "auth_unavailable", "HQ の認証が未接続")
    try:
        who = fn(request)
        if inspect.isawaitable(who):
            who = await who
    except Exception:
        return None, json_error(503, "auth_unavailable", "HQ の認証を確認できない")
    if not who:
        return None, json_error(403, "forbidden", "許可された利用者ではない")
    return who, None


def make_routes(hub: NotifyHub, *, intake: IntakeClient | None = None, control: ApiClient | None = None) -> list[Route]:
    intake = intake or hub.actions.intake
    control = control or ApiClient(service_url("control"))

    async def receipt(request: Request):
        denied = authorize_component(request, request.app.state.hq_settings)
        if denied:
            return error(denied, "component_forbidden")
        try:
            data = await object_body(request)
        except InvalidRequest:
            return error(422, "bad_request")
        lookup = request.url.path.endswith("/lookup")
        allowed = {"delivery_id", "destination"} if lookup else {"delivery_id", "destination", "body", "mode"}
        if set(data) != allowed or not all(isinstance(data.get(k), str) and 0 < len(data[k]) <= 512 for k in ("delivery_id", "destination")):
            return error(422, "bad_request")
        if not lookup and (not isinstance(data["body"], dict) or data["mode"] not in ("run", "collect_only")):
            return error(422, "bad_request")
        if data["destination"] != "user":
            return error(409, "destination_mismatch")
        if lookup:
            result = await run_in_threadpool(hub.dispatcher.receipt_lookup, data["delivery_id"], data["destination"])
        else:
            collect = os.environ.get("AX_HQ_NOTIFY_RUN", "1") == "0" or data["mode"] == "collect_only"
            try:
                result = await run_in_threadpool(hub.dispatcher.receipt_send, data["delivery_id"], data["destination"], data["body"], collect_only=collect)
            except (TypeError, ValueError):
                return error(422, "bad_request")
        return JSONResponse(result, status_code=409 if result["status"] == "conflict" else 200)

    async def status(request: Request):
        who, err = await _authorize(request)
        if err:
            return err
        return JSONResponse(await run_in_threadpool(hub.stats))

    async def open_(request: Request):
        who, err = await _authorize(request)
        if err:
            return err
        res = await run_in_threadpool(hub.actions.open_target, request.path_params["todo_id"])
        return JSONResponse(res, status_code=res["status"])

    async def action(request: Request):
        who, err = await _authorize(request)
        if err:
            return err
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return json_error(400, "bad_json", "本文が JSON でない")
        try:
            invalid = schemas.errors("ext/b10a/notify-action", data)
        except schemas.SchemaError:
            return json_error(503, "schema_unavailable", "操作の形式を確認できない")
        if invalid:
            return json_error(422, "bad_request", "操作の形式が不正")
        res = await run_in_threadpool(hub.actions.apply, todo_id=data["todo_id"], action=data["action"], op_id=data["op_id"],
                                     reason=data.get("reason"), check_at=data.get("check_at"))
        return JSONResponse(res, status_code=res["status"])

    async def test(request: Request):
        who, err = await _authorize(request)
        if err:
            return err
        res = await run_in_threadpool(hub.dispatcher.send_test_once)
        return JSONResponse(res, status_code=res["status"])

    async def scopes(request: Request):
        who, err = await _authorize(request)
        if err:
            return err
        plugins: list[str] = []
        unavailable = False
        try:
            resp = await run_in_threadpool(control.get, "/v1/plugins")
            items = (resp or {}).get("plugins") if isinstance(resp, dict) else resp
            plugins = sorted(str(p.get("id")) for p in (items or []) if isinstance(p, dict) and p.get("id"))
        except (Unavailable, ApiError):
            unavailable = True
        return JSONResponse({"scopes": list(SCOPE_FIXED) + plugins, "plugins_unavailable": unavailable})

    async def emergency(request: Request):
        who, err = await _authorize(request)
        if err:
            return err
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return json_error(400, "bad_json", "本文が JSON でない")
        if not isinstance(data, dict) or not isinstance(data.get("scope"), str) or not data["scope"]:
            return json_error(422, "bad_request", "scope が要る")
        if schemas.errors("ext/b10a/emergency.payload", {"scope": data["scope"]}):
            return json_error(422, "bad_request", "scope の形式が不正")
        if data.get("confirm") is not True:
            return json_error(422, "confirm_required", "確認（confirm: true）が要る。戻しにくい操作")
        op_id = data.get("op_id")
        if not isinstance(op_id, str) or not op_id or len(op_id) > 120:
            return json_error(422, "op_id_required", "op_id が要る")
        try:
            status, body = await run_in_threadpool(intake.submit, "emergency", {"scope": data["scope"]},
                                                   idempotency_key=f"emergency:{op_id}", confirm_irreversible=True)
        except IntakeError:
            return json_error(503, "intake_unavailable", "入口に届かない")
        return JSONResponse(body, status_code=status)

    return [
        Route("/v1/notify/deliveries", receipt, methods=["POST"]),
        Route("/v1/notify/deliveries/lookup", receipt, methods=["POST"]),
        Route("/v1/notify/status", status, methods=["GET"]),
        Route("/v1/notify/open/{todo_id}", open_, methods=["GET"]),
        Route("/v1/notify/actions", action, methods=["POST"]),
        Route("/v1/notify/test", test, methods=["POST"]),
        Route("/v1/emergency/scopes", scopes, methods=["GET"]),
        Route("/v1/emergency", emergency, methods=["POST"]),
    ]


def register(app: Any) -> None:
    hub = build_hub(sink=getattr(app.state, "sink", None), redactor=_redactor_from_env())
    app.state.notify = hub
    app.router.routes.extend(r for r in make_routes(hub) if not r.path.startswith("/v1/notify/deliveries"))
    if os.environ.get("AX_HQ_NOTIFY_RUN", "1") != "0":
        runner = Runner(hub)
        previous_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def notify_lifespan(application):
            async with previous_lifespan(application) as state:
                runner.start()
                try:
                    yield state
                finally:
                    runner.stop()

        app.router.lifespan_context = notify_lifespan
        app.state.notify_runner = runner


def receipt_app(hub, settings):
    from starlette.applications import Starlette

    routes = [r for r in make_routes(hub) if r.path in {"/v1/notify/deliveries", "/v1/notify/deliveries/lookup"}]
    from core.hq.change_approval import routes as approval_routes
    routes.extend(approval_routes())
    app = Starlette(routes=routes)
    app.state.hq_settings = settings
    return app

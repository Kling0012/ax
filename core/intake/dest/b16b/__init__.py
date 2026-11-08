"""既存の2つの行き先に S-23.8 の開始指示を足す。"""
from __future__ import annotations

from urllib.parse import urlsplit

from axcommon import schemas
from axcommon.client import ApiClient
from axcommon.creds import read_secret_optional
from core.intake.app import api_from_handler
from core.intake.dest.b16b.common import PLUGIN_IDS


def register(app):
    handlers = app.state.intake.dest_handlers
    previous = handlers.get("control.command")

    def permission_change(app, dest, body, ident):
        payload = body.get("payload") or {}
        if ident.from_ != "hq":
            return 202, {"ok": False, "reason": "approval_required"}
        # 制御 API が一覧と全操作を検証してから1件だけ登録する。
        command = dict(payload)
        command["issued_by"] = "user"
        u = urlsplit(dest["handler"])
        client = ApiClient(f"{u.scheme}://{u.netloc}", token=read_secret_optional("core-token-intake"),
                           transport=app.state.intake.transport)
        return 201, client.post(u.path, command)

    def control_command(app, dest, body, ident):
        payload = body.get("payload") or {}
        args = payload.get("args") or {}
        is_start = False
        if payload.get("op") == "set_mode" and args.get("mode") == "run":
            client, _ = api_from_handler(dest["handler"], transport=app.state.intake.transport)
            plugins = client.get("/v1/plugins")["plugins"]
            is_start = payload.get("target") in PLUGIN_IDS or any(
                p.get("delivery_targets") and payload.get("target") in (p["id"], "all") for p in plugins)
        if is_start:
            # user の資格は入口が確認する。本文の自己申告では変えない。
            if ident.from_ != "hq":
                return 202, {"ok": False, "reason": "approval_required"}
            command = dict(payload)
            command["issued_by"] = "user"
            command.pop("expires_at", None)
            u = urlsplit(dest["handler"])
            client = ApiClient(f"{u.scheme}://{u.netloc}", token=read_secret_optional("core-token-intake"),
                               transport=app.state.intake.transport)
            schemas.validate("control-command-request", command)
            return 200, client.post(u.path, command)
        if previous is not None:
            return previous(app, dest, body, ident)
        client, path = api_from_handler(dest["handler"], transport=app.state.intake.transport)
        return 200, client.post(path, payload)

    handlers["permission.change"] = permission_change
    handlers["control.command"] = control_command
    from core.intake.dest.b16b.preflight import install_preflight
    install_preflight(app)

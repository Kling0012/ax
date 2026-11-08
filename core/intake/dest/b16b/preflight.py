"""開始指示の段を既存の検証処理で判定し、承認前の実行を防ぐ。"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from axcommon import ids, schemas
from axcommon.client import ApiClient
from axcommon.http import json_error
from core.intake.dest.b16b.common import PLUGIN_IDS, TargetRegistry
from core.intake.validation import validate
from core.intake.vias import Ident


class RegistryView:
    def __init__(self, registry, destination):
        self.registry, self.destination = registry, destination

    def get(self, name):
        dest = self.registry.get(name)
        if name == self.destination and dest:
            return {**dest, "tier": "irreversible", "irreversible": True}
        return dest



async def validate_target(state, body):
    destination = body.get("destination")
    payload = body.get("payload")
    if destination != "permission.change" or not isinstance(payload, dict):
        return None
    dest = state.registry.get(destination)
    if dest is None:
        return None
    u = urlsplit(dest["handler"])
    client = ApiClient(f"{u.scheme}://{u.netloc}", transport=state.transport)
    try:
        registry = await asyncio.to_thread(TargetRegistry.from_client, client)
        operations = payload.get("operations")
        if (isinstance(operations, list) and operations
                and set(operations) <= {"merge", "release", "deploy", "post"}
                and not any(t["destination"] == payload.get("destination") for t in registry.targets)):
            return None
        registry.validate_permission(payload)
    except (ValueError, schemas.SchemaError):
        return 422, "target_not_listed"
    except Exception:
        return 503, "target_list_unavailable"
    return None


def install_preflight(app):
    app.state.intake.target_preflight = validate_target
    for route in app.router.routes:
        if getattr(route, "path", None) != "/v1/instructions":
            continue
        original = route.endpoint

        async def endpoint(request, original=original):
            ident = request.scope.get("ax_ident")
            if not isinstance(ident, Ident):
                return await original(request)
            try:
                body = await request.json()
            except ValueError:
                return await original(request)
            if not isinstance(body, dict):
                return await original(request)
            destination = body.get("destination")
            payload = body.get("payload") or {}
            if not isinstance(payload, dict):
                return await original(request)
            mode_run = (destination == "control.command" and payload.get("op") == "set_mode"
                        and isinstance(payload.get("args"), dict) and payload["args"].get("mode") == "run")
            if destination != "permission.change" and not mode_run:
                return await original(request)
            state = app.state.intake
            dest = state.registry.get(destination)
            if dest is None:
                return await original(request)
            instruction_id = body.get("id") or ids.new_ulid()

            def denied(status, code):
                if state.sink is not None:
                    state.sink.emit("instruction.rejected", instruction_id,
                                    {"destination": destination, "reason": code})
                return json_error(status, code, code)

            basic = validate(state.registry, body, ident)
            if basic is not None and basic.status != 202:
                return denied(basic.status, basic.reason)
            u = urlsplit(dest["handler"])
            client = ApiClient(f"{u.scheme}://{u.netloc}", transport=state.transport)
            try:
                registry = await asyncio.to_thread(TargetRegistry.from_client, client)
                if destination == "permission.change":
                    operations = payload.get("operations")
                    if (isinstance(operations, list) and operations
                            and set(operations) <= {"merge", "release", "deploy", "post"}
                            and not any(t["destination"] == payload.get("destination") for t in registry.targets)):
                        return await original(request)
                    registry.validate_permission(payload)
                elif payload.get("target") not in PLUGIN_IDS and not registry.for_plugin(payload.get("target")):
                    if payload.get("target") != "all" or not registry.targets:
                        return await original(request)
            except (ValueError, schemas.SchemaError):
                return denied(422, "target_not_listed")
            except Exception:
                return denied(503, "target_list_unavailable")
            raised = RegistryView(state.registry, destination)
            rejection = validate(raised, body, ident)
            if rejection is not None:
                if rejection.reason == "approval_required" and state.approval_check is not None:
                    try:
                        result = await asyncio.to_thread(state.approval_check, body, raised.get(destination), ident)
                    except Exception:
                        return denied(503, "approval_unavailable")
                    if result is not None:
                        rejection = result
                # 承認を保存した場合も、同じ要求の中では実行しない。
                return denied(rejection.status, rejection.reason)
            return await original(request)

        from starlette.routing import request_response
        route.endpoint = endpoint
        route.app = request_response(endpoint)

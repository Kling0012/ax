"""S-23.8 の送信対象と許可の照合。秘密と旧キューは扱わない。"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path

from axcommon import publish_legacy_plugin_ids, schemas

OPERATIONS = frozenset(("投稿", "送信", "publish", "reply", "respond", "friend_request", "friend_accept"))
PLUGIN_IDS = frozenset(("x-posts", "discord-forward", "scheduled-publisher"))


def canonical_plugin_id(plugin_id):
    return "scheduled-publisher" if plugin_id in publish_legacy_plugin_ids() else plugin_id


class ScopeError(ValueError):
    pass


class TargetRegistry:
    def __init__(self, targets):
        schemas.validate("ext/b16b/delivery-targets", {"targets": targets})
        self.targets = deepcopy(targets)
        for target in self.targets:
            target["plugin_id"] = canonical_plugin_id(target["plugin_id"])
            if not set(target["operations"]) <= OPERATIONS:
                raise ScopeError("unregistered_operation")
            if any(not str(target[k]).strip() for k in ("plugin_id", "project", "repo", "destination")):
                raise ScopeError("empty_target")

    @classmethod
    def load(cls, path=None):
        path = Path(path or os.environ.get("AX_AUTOMATION_TARGETS", "/state/control/automation-targets.json"))
        if not path.exists():
            return cls([])
        body = json.loads(path.read_text(encoding="utf-8"))
        schemas.validate("ext/b16b/delivery-targets", body)
        return cls(body["targets"])

    @classmethod
    def from_client(cls, client):
        response = client.get("/v1/plugins")
        return cls([target for plugin in response["plugins"]
                    for target in plugin.get("delivery_targets", [])])

    def for_plugin(self, plugin_id):
        return [deepcopy(t) for t in self.targets if t["plugin_id"] == plugin_id]

    def validate_permission(self, permission, plugin_id=None):
        schemas.validate("ext/b16b/permission-change", {
            k: v for k, v in permission.items()
            if k not in ("created_at", "updated_at", "created_by")
        })
        ops = permission["operations"]
        if any(not str(permission[k]).strip() for k in ("project", "repo", "destination", "source_instruction")):
            raise ScopeError("target_required")
        matches = [t for t in self.targets
                   if (plugin_id is None or t["plugin_id"] == plugin_id)
                   and all(t[k] == permission[k] for k in ("project", "repo", "destination"))
                   and set(ops) <= set(t["operations"])]
        if not matches:
            raise ScopeError("target_not_listed")
        return matches

    def contains(self, target, operation):
        return any(all(t[k] == target.get(k) for k in ("plugin_id", "project", "repo", "destination"))
                   and operation in t["operations"] for t in self.targets)


class PermissionReader:
    """既存の API を送信の直前に呼ぶ。結果をキャッシュしない。"""
    def __init__(self, client):
        self.client = client

    def check(self, target, operation):
        body = {k: target[k] for k in ("project", "repo", "destination")}
        body["operation"] = operation
        try:
            result = self.client.post("/v1/permissions/check", body)
            return isinstance(result, dict) and result.get("allowed") is True
        except Exception:
            return False


class DeliveryScopeValidator:
    """B3 の検証中に呼ぶ拡張口。成功は None、拒否は Reject。"""
    def __init__(self, service, registry_provider=TargetRegistry.load):
        self.service, self.registry_provider = service, registry_provider

    def __call__(self, command, body, plugin):
        from core.control import Reject, SENDING_OUTPUTS

        def reject(code):
            self.service._rejected(command.get("id", plugin["id"]), body, code)
            raise Reject(400, code, code, rule="9")

        if plugin["id"] not in PLUGIN_IDS and not (
                set(plugin.get("manifest", {}).get("outputs") or []) & SENDING_OUTPUTS):
            return None
        try:
            registry = self.registry_provider()
        except (ValueError, OSError, schemas.SchemaError):
            reject("target_list_unavailable")
            return None
        if plugin["id"] not in PLUGIN_IDS and not registry.for_plugin(plugin["id"]):
            return None
        if command.get("op") != "set_mode" or command.get("args", {}).get("mode") != "run":
            return None
        if command.get("issued_by") != "user":
            reject("user_only")
        permission_ids = command["args"].get("permission_ids")
        if not isinstance(permission_ids, list) or not permission_ids:
            reject("permission_ids_required")
        try:
            for permission_id in permission_ids:
                permission = self.service.store.get_permission(permission_id) if isinstance(permission_id, str) else None
                if not permission or permission.get("enabled") is not True:
                    reject("permission_invalid")
                registry.validate_permission(permission, plugin["id"])
                if not command.get("corr") or command["corr"] != permission["source_instruction"]:
                    reject("source_instruction_mismatch")
        except (ValueError, OSError, schemas.SchemaError):
            reject("target_not_listed")
        return None


def install_control(service, registry_provider=TargetRegistry.load):
    """B3 のインスタンスだけに検証を足す。B3 のファイルとクラスは変更しない。"""
    from core.control import OPERATION_KINDS, SENDING_OUTPUTS, Reject

    if getattr(service, "b16b_installed", False):
        return service
    OPERATION_KINDS.update(OPERATIONS)
    SENDING_OUTPUTS.update(OPERATIONS)
    original_issue = service.issue
    original_validate = service._validate
    original_targets = service._targets_of
    checked_targets = ContextVar("b16b_checked_targets", default=frozenset())
    original_create = service.create_permission
    original_disable = service.disable_permission
    original_check = service.check_permission
    original_view = service.plugin_view
    original_effective = service.effective

    def effective(plugin_id):
        result = original_effective(plugin_id)
        try:
            sending = plugin_id in PLUGIN_IDS or bool(registry_provider().for_plugin(plugin_id))
        except (ValueError, OSError, schemas.SchemaError):
            sending = plugin_id in PLUGIN_IDS
        if sending and result["mode"] == "run":
            started = any(c["target"] in (plugin_id, "all") and c["op"] == "set_mode"
                          and c["args"].get("mode") == "run" and c["issued_by"] == "user"
                          and c["args"].get("permission_ids") and c.get("corr")
                          for c in service.store.living_commands())
            if not started:
                result["mode"] = "collect_only"
        return result

    def plugin_view(plugin_id):
        view = original_view(plugin_id)
        if view is not None:
            try:
                view["delivery_targets"] = registry_provider().for_plugin(plugin_id)
            except (ValueError, OSError, schemas.SchemaError):
                view["delivery_targets"] = []
        return view

    def reject(body, code):
        service._rejected(body.get("target", "permission"), body, code)
        raise Reject(400, code, code, rule="9")

    def create_permission(body, *, by):
        if by != "user":
            raise Reject(403, "user_only", "user_only")
        body = {k: v for k, v in body.items() if k != "issued_by"}
        if set(body.get("operations") or []) & OPERATIONS:
            try:
                registry_provider().validate_permission(body)
            except (ValueError, OSError, schemas.SchemaError) as exc:
                raise Reject(422, "target_not_listed", "target_not_listed") from exc
        if body.get("enabled") is False:
            permission_id = body.get("id")
            existing = service.store.get_permission(permission_id) if isinstance(permission_id, str) else None
            if existing is None or any(existing.get(k) != body.get(k) for k in ("project", "repo", "destination")):
                raise Reject(422, "permission_invalid", "permission_invalid")
            return original_disable(permission_id, by=by)
        return original_create(body, by=by)

    def disable_permission(permission_id, *, by):
        if by != "user":
            raise Reject(403, "user_only", "user_only")
        return original_disable(permission_id, by=by)

    def validate_scope(body, *, issued_by):
        processes = []
        args = body.get("args") or {}
        if body.get("op") == "set_mode" and args.get("mode") == "run":
            try:
                targets = original_targets(body.get("target"))
                try:
                    registry = registry_provider()
                except (ValueError, OSError, schemas.SchemaError):
                    if any(p["id"] in PLUGIN_IDS for p in targets) or body.get("target") in PLUGIN_IDS:
                        reject(body, "target_list_unavailable")
                    return frozenset()
                processes = [p["id"] for p in targets
                             if p["id"] in PLUGIN_IDS or registry.for_plugin(p["id"])]
                if body.get("target") in PLUGIN_IDS and not targets:
                    reject(body, "unknown_target")
                if processes:
                    if issued_by != "user":
                        reject(body, "user_only")
                    permission_ids = args.get("permission_ids")
                    if not isinstance(permission_ids, list) or not permission_ids:
                        reject(body, "permission_ids_required")
                    for plugin_id in processes:
                        for permission_id in permission_ids:
                            permission = service.store.get_permission(permission_id) if isinstance(permission_id, str) else None
                            if not permission or permission.get("enabled") is not True:
                                reject(body, "permission_invalid")
                            registry.validate_permission(permission, plugin_id)
                            if not body.get("corr") or body["corr"] != permission["source_instruction"]:
                                reject(body, "source_instruction_mismatch")
            except (ValueError, OSError, schemas.SchemaError):
                reject(body, "target_not_listed")
        return frozenset(processes)

    def targets_for_validation(target):
        checked = checked_targets.get()
        targets = original_targets(target)
        return [{**p, "_declared_outputs": p["manifest"].get("outputs"),
                 "manifest": {**p["manifest"], "outputs": []}} if p["id"] in checked else p
                for p in targets]

    def validate(command, body):
        # 基底の「一覧未接続」の停止は、今回照合済みの対象だけで解消する。
        # ContextVar は検証中だけ有効で、保存する manifest と他のスレッドを変えない。
        if (command["op"] == "set_mode" and command["args"].get("mode") == "run"
                and command["target"] != "all"):
            targets = original_targets(command["target"])
            if any("run" not in (p["manifest"].get("capabilities") or []) for p in targets):
                return original_validate(command, body)
        checked = validate_scope(body, issued_by=command["issued_by"])
        token = checked_targets.set(checked)
        try:
            return original_validate(command, body)
        finally:
            checked_targets.reset(token)

    def check_permission(body):
        if body.get("operation") in OPERATIONS:
            # B3 のワイルドカード許可を外部送信には使わない。
            try:
                registry = registry_provider()
                for permission in service.store.list_permissions():
                    if permission.get("enabled") is not True:
                        continue
                    if any(permission.get(k) != body.get(k) for k in ("project", "repo", "destination")):
                        continue
                    if body.get("operation") not in permission.get("operations", []):
                        continue
                    registry.validate_permission(permission)
                    service.emit("permission.checked", body["repo"], {
                        "permission_id": permission["id"], "operation": body["operation"], "result": "allowed"})
                    return {"allowed": True, "permission_id": permission["id"], "reason": "listed_permission"}
            except (ValueError, OSError, schemas.SchemaError):
                pass
            service.emit("permission.checked", body.get("repo", "-"), {
                "permission_id": None, "operation": body.get("operation"), "result": "denied"})
            return {"allowed": False, "permission_id": None, "reason": "permission_denied"}
        return original_check(body)

    def read_delivery_permission(plugin_id, target, operation):
        try:
            from core.control import PRIORITY
            registry = registry_provider()
            if operation not in OPERATIONS or not registry.contains({**target, "plugin_id": plugin_id}, operation):
                return False
            plugin = service.store.get_plugin(plugin_id)
            if not plugin or service.effective(plugin_id)["mode"] != "run":
                return False
            commands = sorted(service.store.living_commands(),
                              key=lambda c: (c.get("issued_at", ""), c.get("seq") or 0), reverse=True)
            commands.sort(key=lambda c: PRIORITY.get(c.get("issued_by"), 99))
            for command in commands:
                if (command.get("target") not in (plugin_id, "all") or command.get("op") != "set_mode"
                        or command.get("args", {}).get("mode") not in plugin["manifest"].get("capabilities", [])):
                    continue
                if (command.get("issued_by") != "user" or command["args"].get("mode") != "run"
                        or not command.get("corr")):
                    return False
                for permission_id in command["args"].get("permission_ids", []):
                    permission = service.store.get_permission(permission_id) if isinstance(permission_id, str) else None
                    if (not permission or permission.get("enabled") is not True
                            or permission.get("source_instruction") != command["corr"]
                            or any(permission.get(k) != target.get(k) for k in ("project", "repo", "destination"))
                            or operation not in permission.get("operations", [])):
                        continue
                    registry.validate_permission(permission, plugin_id)
                    return True
                return False
        except Exception:
            return False
        return False

    service.read_delivery_permission = read_delivery_permission
    service.validate_delivery_scope = DeliveryScopeValidator(service, registry_provider)
    service.effective = effective
    service.plugin_view = plugin_view
    service._targets_of = targets_for_validation
    service._validate = validate
    service.issue = original_issue
    service.create_permission = create_permission
    service.disable_permission = disable_permission
    service.check_permission = check_permission
    service.b16b_installed = True
    return service

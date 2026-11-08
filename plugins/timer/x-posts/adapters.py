"""Control adapter using the existing B3/B16b contracts, without new endpoints."""
from __future__ import annotations

from core.intake.dest.b16b.common import PermissionReader, TargetRegistry


class ControlAdapter:
    def __init__(self, client, registry_provider=None):
        self.client = client
        self.registry_provider = registry_provider
        self.permissions = PermissionReader(client)

    def targets(self):
        if self.registry_provider is not None:
            return self.registry_provider().for_plugin("x-posts")
        view = self.client.get("/v1/plugins/x-posts")
        return TargetRegistry(view["delivery_targets"]).for_plugin("x-posts")

    def effective(self, plugin_id):
        return self.client.effective(plugin_id)

    def check(self, target, operation):
        if not self.permissions.check(target, operation):
            return False
        try:
            commands = self.client.get("/v1/commands", params={"state": "active", "issued_by": "user"})["commands"]
            permissions = self.client.get("/v1/permissions")["permissions"]
            # The most recent user mode command defines the started scope, not every enabled permission.
            command = next((value for value in reversed(commands)
                            if value.get("target") in (target["plugin_id"], "all")
                            and value.get("issued_by") == "user" and value.get("state") == "active"
                            and value.get("op") == "set_mode"), None)
            if command is None or command.get("args", {}).get("mode") != "run":
                return False
            started = command["args"].get("permission_ids", [])
            return any(permission.get("id") in started and permission.get("enabled") is True
                       and permission.get("source_instruction") == command.get("corr")
                       and not permission.get("conditions")
                       and all(permission.get(key) == target[key] for key in ("project", "repo", "destination"))
                       and operation in permission.get("operations", []) for permission in permissions)
        except Exception:
            return False


class UnboundEnvironment:
    def run(self, request, artifacts):
        raise NotImplementedError("Bind B14b2 env.run with artifact staging; never invoke a host CLI here")

    def lookup(self, job_id):
        raise NotImplementedError("Bind B14b2 receipt lookup; missing receipt remains unknown")

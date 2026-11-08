"""Adapter contract for independently identified temporary exclusions."""


class ExclusionAdapter:
    def __init__(self, client):
        self.client = client

    def sync(self, command, *, active):
        if active:
            self.client.exclude(command_id=command["id"], resource_id=command["args"]["resource_id"],
                                until=command["args"]["until"], reason=command["reason"])
        else:
            self.client.release(command_id=command["id"], resource_id=command["args"]["resource_id"])


class ResourcesExclusionClient:
    def __init__(self, client):
        self.client = client

    def exclude(self, *, command_id, resource_id, until, reason):
        result = self.client.post("/v1/command-exclusions", {"command_id": command_id, "resource_id": resource_id,
                                  "until": until, "reason": reason, "active": True})
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("exclusion not acknowledged")

    def release(self, *, command_id, resource_id):
        result = self.client.post("/v1/command-exclusions", {"command_id": command_id, "resource_id": resource_id,
                                  "active": False})
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("release not acknowledged")

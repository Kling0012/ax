"""Stateful owner fakes: no transports, keys, services, or provider calls."""
from copy import deepcopy
import threading

from core.resources.ext.b18b import IntegrationError, WorkerPorts


def box(env_id="box", **overrides):
    row = {"id": env_id, "kind": "environment", "adapter": "cloud-sandbox",
           "capabilities": ["linux-sandbox", "web-screenshot", "sandbox-desktop"],
           "data_classes": ["public"], "state": "available", "gate_state": "available",
           "gate_ready": True, "slot_free": True, "cost": "free", "rank": 100,
           "day_use_s": 0, "registered_at": "2026-01-01T00:00:00Z",
           "probe": {"capabilities_checked": {"linux-sandbox": "ok", "web-screenshot": "ok",
                                              "sandbox-desktop": "ok"}},
           "free_command": True, "entrypoints": {"screenshot": ["expand:config/sandbox-entry-steps.yaml#screenshot"]},
           "implemented_tools": ["browser_navigate", "screenshot"]}
    row.update(deepcopy(overrides))
    return row


def worker_env(env_id="remote", **overrides):
    row = {"id": env_id, "kind": "environment", "adapter": "ssh-host",
           "capabilities": ["worker-host"], "data_classes": ["public", "private", "sensitive"],
           "state": "available", "worker": {"runners": ["registered-runner"], "isolation": "container", "egress": ["model-gateway"]},
           "model_family": ["glm"], "cost": "fixed", "rank": 100, "day_use_s": 0,
           "registered_at": "2026-01-01T00:00:00Z"}
    row.update(deepcopy(overrides))
    return row


def model(model_id="glm/test", **overrides):
    row = {"id": model_id, "kind": "model", "status": "active", "family": "glm", "level": "L3",
           "tier": "standard", "billing": "flat", "runner": "registered-runner", "vision": False, "trains_on_input": True}
    row.update(deepcopy(overrides))
    return row


class Owner:
    def __init__(self):
        self.lock = threading.RLock()
        self.leases, self.children, self.events, self.calls = {}, {}, [], []
        self.model_used = self.env_used = 0
        self.model_cap = self.env_cap = 1
        self.isolation_ok = True
        self.scan_blocked = False
        self.submit_error = None
        self.result = {"state": "running"}
        self.releases = 0

    def ports(self):
        return WorkerPorts(self.reserve_pair, self.release_pair, self.submit, self.record_child,
                           self.scan_outbound, self.scan_inbound, lambda env: self.isolation_ok)

    def reserve_pair(self, model_id, env_id, parent, child):
        with self.lock:
            if self.model_used >= self.model_cap:
                return False
            self.model_used += 1
            if self.env_used >= self.env_cap:
                self.model_used -= 1
                return False
            self.env_used += 1
            self.leases[(parent, child)] = (model_id, env_id)
            return True

    def release_pair(self, parent, child):
        with self.lock:
            if (parent, child) in self.leases:
                self.leases.pop((parent, child))
                self.model_used -= 1
                self.env_used -= 1
                self.releases += 1

    def scan_outbound(self, spec):
        self.calls.append(("scan_outbound", deepcopy(spec)))
        if self.scan_blocked:
            raise ValueError("blocked")

    def scan_inbound(self, result):
        if "forbidden-output" in str(result):
            raise ValueError("blocked")

    def submit(self, env_id, spec):
        self.calls.append(("submit", env_id, deepcopy(spec)))
        if self.submit_error:
            raise self.submit_error
        return deepcopy(self.result)

    def record_child(self, child, state, result):
        self.children[child] = {"state": state, "result": deepcopy(result)}


class RouteOwner:
    def __init__(self):
        self.rows = {"hq-public": {"id": "hq-public", "state": "confirmed", "kind": "public-entry"}}
        self.calls = []
        self.probe_ok = False

    def register(self, row):
        self.calls.append(("register", deepcopy(row)))
        self.rows[row["id"]] = deepcopy(row)
        return self.rows[row["id"]]

    def probe(self, route_id):
        self.calls.append(("probe", route_id))
        assert self.rows[route_id]["state"] == "candidate"
        return {"ok": self.probe_ok, "checked_at": "2026-01-01T00:00:00Z", "source": "fake-probe"}

    def confirm(self, route_id, evidence):
        assert evidence["ok"]
        self.calls.append(("confirm", route_id))
        self.rows[route_id]["state"] = "confirmed"
        return deepcopy(self.rows[route_id])


class GateOwner:
    def __init__(self, rows):
        self.rows, self.events, self.issued = rows, [], []
        self.busy = set()
        self.env_slots = 0

    def emit(self, kind, data):
        self.events.append((kind, deepcopy(data)))

    def issue(self, env_id, request):
        self.issued.append(env_id)
        if env_id in self.busy:
            raise IntegrationError(409, "slot_busy")
        self.env_slots += 1
        return {"test_gate_handle": env_id}

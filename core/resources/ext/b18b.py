"""B18b assembly and offline-testable orchestration (S-25.3/9/10/11).

No transports or credentials are constructed here. Required owner ports must be
bound explicitly; an absent port raises NotImplementedError for that feature.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Mapping
import uuid

from axcommon.policy import SENSITIVITIES, allowed

_ID = re.compile(r"[a-z0-9-]{1,40}\Z")
_GATE_CAPS = frozenset({"linux-sandbox", "web-screenshot", "web-gui", "sandbox-desktop"})
_ENV_TERMINAL = frozenset({"succeeded", "failed", "timed_out", "stalled", "cancelled", "lost", "rejected"})
_ENV_STATES = _ENV_TERMINAL | {"queued", "running"}
_POOL = "plugins.resource.worker-pool"


class IntegrationError(ValueError):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def _missing(*_args, **_kwargs):
    raise NotImplementedError("b18b owner port is not bound")


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise IntegrationError(422, "bad_request")
    return value


def _bool(value: Any) -> bool:
    if value in (True, "true", "1"):
        return True
    if value in (False, "false", "0"):
        return False
    raise IntegrationError(422, "bad_request")


def _csv(value: Any) -> list[str]:
    if not isinstance(value, str) or not value or any(not part for part in value.split(",")):
        raise IntegrationError(422, "bad_request")
    parts = value.split(",")
    if any(not re.fullmatch(r"[a-z][a-z0-9:-]*", part) for part in parts):
        raise IntegrationError(422, "bad_request")
    return list(dict.fromkeys(parts))


def selection_query(query: Mapping) -> dict:
    """Parse the documented GET query, without side effects or provider calls."""
    path = query.get("path", "J")
    if path not in ("J", "G") or query.get("sensitivity") not in SENSITIVITIES:
        raise IntegrationError(422, "bad_request")
    result = {"path": path, "sensitivity": query["sensitivity"]}
    if path == "J":
        result.update(requires=_csv(query.get("requires")),
                      user_requested=_bool(query.get("user_requested", "false")))
        for key in ("entry_kind", "entry_name"):
            if key in query:
                result[key] = query[key]
        if result.get("entry_kind") not in (None, "entrypoint", "command", "steps", "worker", "instruction"):
            raise IntegrationError(422, "bad_request")
    else:
        if "requires" in query or not query.get("role"):
            raise IntegrationError(422, "bad_request")
        result.update(tools=_csv(query.get("tools")), role=query["role"],
                      needs_vision=_bool(query.get("needs_vision", "false")))
        record = query.get("record", "0")
        if record not in ("0", "1"):
            raise IntegrationError(422, "bad_request")
        result["record"] = record == "1"
        if result["record"] and not query.get("job_id"):
            raise IntegrationError(422, "bad_request")
        for key in ("job_id", "trial_for"):
            if key in query:
                result[key] = _identifier(query[key])
    return result


def selection_authorize(*, path: str, side: str, credential: str | None,
                        authorize_j: Callable[[str, Mapping], bool], query: Mapping) -> None:
    if credential is None:
        raise IntegrationError(401, "unauthenticated")
    if path == "G":
        if side != "core" or credential != "worker-pool":
            raise IntegrationError(403, "credential_scope")
    elif credential == "worker-pool" or not authorize_j(credential, query):
        raise IntegrationError(403, "credential_scope")


def _environment_order(env: dict, index: int) -> tuple:
    cost = env.get("cost", {})
    cost_kind = cost.get("kind") if isinstance(cost, dict) else cost
    return ({"free": 0, "fixed": 1, "metered": 2}.get(cost_kind, 3),
            env.get("rank", 100), env.get("day_use_s", 0),
            env.get("registered_at", ""), index)


class EnvironmentSelection:
    """One selector for J and G. Snapshots are registry data, never probes.

    snapshots() must return current catalogue rows, including probe and shared
    slot_free. entry_supported handles non-screenshot entry shapes in the owner.
    emit receives metadata only. Neither callback may wake a box or read a key.
    """
    def __init__(self, *, snapshots: Callable = _missing, emit: Callable = _missing,
                 entry_supported: Callable = _missing):
        self.snapshots, self.emit, self.entry_supported = snapshots, emit, entry_supported

    def select(self, query: Mapping) -> dict:
        request = selection_query(query)
        path = request["path"]
        required = request["tools" if path == "G" else "requires"]
        candidates, excluded, ordered = [], [], []
        for index, original in enumerate(self.snapshots()):
            env = deepcopy(original)
            env_id = env["id"]
            reason = None
            state = env.get("gate_state" if path == "G" else "state")
            if state == "trial":
                if path != "G" or request.get("trial_for") != env_id:
                    reason = "trial_excluded"
            elif state != "available":
                reason = "gate_state_not_available" if path == "G" else "state_not_available"
            checked = (env.get("probe") or {}).get("capabilities_checked", {})
            capabilities = set(env.get("capabilities", []))
            if reason is None and (not set(required) <= capabilities or
                    (env.get("adapter") == "cloud-sandbox" and
                     any(checked.get(cap) != "ok" for cap in required))):
                reason = "capability_unverified"
            if reason is None and (request["sensitivity"] not in env.get("data_classes", []) or
                    not allowed(request["sensitivity"], env) or
                    (env.get("owner_use", False) and not request.get("user_requested"))):
                reason = "sensitivity_denied"
            if reason is None and path == "G":
                if request["role"] not in ("research", "screen", "assist"):
                    reason = "role_denied"
                elif request["sensitivity"] != "public":
                    reason = "sensitivity_denied"
                elif (env.get("adapter") != "cloud-sandbox" or
                      not set(required) <= _GATE_CAPS or
                      ("sandbox-desktop" in required and not request["needs_vision"]) or
                      not env.get("gate_ready", False)):
                    reason = "gate_not_ready"
            impl = None
            if reason is None and path == "J" and request.get("entry_kind"):
                if request.get("entry_name") == "screenshot" and request["entry_kind"] == "entrypoint":
                    from core.resources.screenshot.entry_expand import select_implementation
                    shape = {"requires": required, "entry": {"kind": "entrypoint", "name": "screenshot"}}
                    choice = select_implementation(env, shape, env.get("probe", {}),
                                                   set(env.get("implemented_tools", [])))
                    impl = choice.impl
                    if impl is None:
                        reason = "entry_unsupported"
                elif not self.entry_supported(env, request):
                    reason = "entry_unsupported"
            if reason is None and not env.get("slot_free", False):
                reason = "slot_busy"
            if reason is not None:
                excluded.append({"env_id": env_id, "reason": reason})
                continue
            candidate = {"env_id": env_id, "day_use_s": env.get("day_use_s", 0), "slot_free": True}
            if impl is not None:
                candidate["impl"] = impl
            ordered.append((_environment_order(env, index), candidate))
        for order, (_, candidate) in enumerate(sorted(ordered, key=lambda item: item[0]), 1):
            candidates.append({**candidate, "order": order})
        result = {"path": path, "selected": candidates[0]["env_id"] if candidates else None,
                  "candidates": candidates, "excluded": excluded, "recorded": False}
        if path == "G" and request["record"]:
            self.emit("env.selection", {"path": "G", "job_id": request["job_id"],
                      "candidates": deepcopy(candidates), "excluded": deepcopy(excluded),
                      "sensitivity": request["sensitivity"]})
            result["recorded"] = True
        return result


class GateChoice:
    """Single attempt; the pool's deadline scheduler calls again while queued."""
    def __init__(self, *, select: Callable, issue: Callable):
        self.select, self.issue = select, issue

    def acquire(self, request: Mapping, *, deadline_expired: bool = False) -> dict:
        query = {"path": "G", "tools": ",".join(request.get("tools", [])),
                 "sensitivity": request["sensitivity"], "role": request["role"],
                 "needs_vision": "true" if request.get("needs_vision") else "false",
                 "job_id": request["job_id"], "record": "1"}
        if "trial_for" in request:
            query["trial_for"] = request["trial_for"]
        selected = self.select(query)
        if not selected["candidates"]:
            return {"state": "parked", "reason": "no_candidate"}
        for candidate in selected["candidates"]:
            env_id = candidate["env_id"]
            try:
                token = self.issue(env_id, deepcopy(dict(request)))
            except IntegrationError as error:
                if error.status == 409 and error.code == "slot_busy":
                    continue
                raise
            return {"state": "ready", "env_id": env_id, "gate": token}
        return {"state": "parked" if deadline_expired else "queued", "reason": "slot_busy"}


class WorkerLinks:
    """Durable parent/child correspondence; provider slots remain owner-managed."""
    def __init__(self, path: str | Path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS b18b_worker_links ("
                        "parent TEXT PRIMARY KEY, child TEXT UNIQUE NOT NULL, env TEXT NOT NULL, "
                        "model TEXT NOT NULL, state TEXT NOT NULL, result TEXT, released INTEGER NOT NULL DEFAULT 0)")

    def close(self):
        self.db.close()

    def get(self, parent: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM b18b_worker_links WHERE parent=?", (parent,)).fetchone()
            return dict(row) if row else None

    def add(self, parent: str, child: str, env: str, model: str):
        self.db.execute("INSERT INTO b18b_worker_links(parent,child,env,model,state) VALUES(?,?,?,?,?)",
                        (parent, child, env, model, "queued"))

    def update(self, parent: str, state: str, result: dict | None = None, *, released: bool = False):
        self.db.execute("UPDATE b18b_worker_links SET state=?, result=?, released=? WHERE parent=?",
                        (state, json.dumps(result) if result is not None else None, int(released), parent))


@dataclass(frozen=True)
class WorkerPorts:
    """Owner ports. reserve_pair is ONE atomic owner transaction, or changes nothing.

    reserve_pair(model, env, parent, child) -> bool also enforces live model/family
    caps. release_pair(parent, child) is idempotent and returns both slots once.
    submit(env, spec), record_child(child, state, result), scan_outbound(spec),
    scan_inbound(result), isolation(env) are mandatory, never permissive defaults.
    No credential values are passed through any of these ports.
    """
    reserve_pair: Callable = _missing
    release_pair: Callable = _missing
    submit: Callable = _missing
    record_child: Callable = _missing
    scan_outbound: Callable = _missing
    scan_inbound: Callable = _missing
    isolation: Callable = _missing


class WorkerBridge:
    def __init__(self, *, parents: Any, links: WorkerLinks, ports: WorkerPorts):
        self.parents, self.links, self.ports = parents, links, ports

    def start(self, parent_id: str, request: Mapping, *, models: list[dict], environments: list[dict],
              selection_options: Mapping | None = None) -> dict:
        _identifier(parent_id)
        request = deepcopy(dict(request))
        if self.parents.get(parent_id) is None:
            raise IntegrationError(404, "not_found")
        if request.get("placement") not in ("env", "any"):
            raise IntegrationError(422, "bad_request")
        if request.get("sensitivity") not in SENSITIVITIES:
            raise IntegrationError(422, "bad_request")
        if (request.get("irreversible", False) or set(request.get("secrets", [])) - {"model_gateway"}):
            self.parents.set_state(parent_id, "rejected", reason="worker_request_denied")
            return {"state": "rejected", "reason": "worker_request_denied"}
        verify = request.get("verify")
        if isinstance(verify, str):
            verify = {"cmd": verify}
        if not isinstance(verify, dict) or not isinstance(verify.get("cmd"), str) or not verify["cmd"].strip():
            raise IntegrationError(422, "bad_request")
        # Reuse S-8.4, including automatic Claude restrictions and tier policy.
        select_models = importlib.import_module(_POOL + ".selection").select
        options = dict(selection_options or {})
        options.update(role=request.get("role"), sensitivity=request["sensitivity"],
                       needs_vision=request.get("needs_vision", False))
        pairs = []
        for index, env in enumerate(environments):
            if (env.get("state") != "available" or "worker-host" not in env.get("capabilities", []) or
                    not env.get("worker") or
                    request["sensitivity"] not in env.get("data_classes", []) or
                    not allowed(request["sensitivity"], env) or
                    (env.get("owner_use", False) and not request.get("user_requested"))):
                continue
            if not self.ports.isolation(env):
                continue
            eligible = [m for m in models if m.get("family") in env.get("model_family", [])
                        and m.get("runner") in env["worker"].get("runners", [])]
            result = select_models(eligible, **options)
            if result["selected"]:
                pairs.append((env, result["selected"], index))
        # Preserve global model preference instead of preferring a cheap environment
        # with a more expensive model. The S-8.4 selector chooses across valid pairs.
        paired_ids = {model for _, model, _ in pairs}
        chosen = select_models([m for m in models if m["id"] in paired_ids], **options)["selected"]
        pairs = [(env, model, index) for env, model, index in pairs if model == chosen]
        pairs.sort(key=lambda item: (_environment_order(item[0], item[2])[:-1],
                   0 if request["placement"] == "any" and item[0]["id"] == os.environ.get("AX_WORKER_LOCAL_ENV_ID") else 1,
                   item[2]))
        with self.links.lock:
            previous = self.links.get(parent_id)
            if previous:
                return {"state": previous["state"], "job_id": previous["child"], "env_id": previous["env"]}
            if not pairs:
                self.parents.set_state(parent_id, "parked", reason="no_worker_environment")
                return {"state": "parked", "reason": "no_worker_environment"}
            env, model, _ = pairs[0]
            child = "env-" + uuid.uuid4().hex
            spec = {"job_id": child, "requires": ["worker-host"], "sensitivity": request["sensitivity"],
                    "entry": {"kind": "worker", "name": "ax-worker-launch",
                              "args": {"model": model, "role": request["role"],
                                       "prompt": request.get("prompt", "")}},
                    "verify": deepcopy(verify), "irreversible": False,
                    "secrets": deepcopy(request.get("secrets", [])),
                    "inputs": deepcopy(request.get("inputs", [])),
                    "outputs": ["changes.bundle", "report.md"],
                    "budget_min": request.get("budget_min", 60), "stall_min": request.get("stall_min", 15)}
            self.ports.scan_outbound(deepcopy(spec))
            if not self.ports.reserve_pair(model, env["id"], parent_id, child):
                self.parents.set_state(parent_id, "queued", reason="slot_busy")
                return {"state": "queued", "reason": "slot_busy"}
            try:
                self.links.add(parent_id, child, env["id"], model)
            except Exception:
                self.ports.release_pair(parent_id, child)
                raise
            self.parents.set_state(parent_id, "launching", model=model)
            self.ports.record_child(child, "queued", None)
            try:
                result = self.ports.submit(env["id"], deepcopy(spec))
            except Exception:
                # A disconnect after submission is ambiguous. Retain both slots
                # until reconciliation; never submit to another environment.
                result = {"state": "lost", "error": "submit_uncertain"}
            return self._accept(parent_id, result)

    def complete(self, parent_id: str, result: Mapping) -> dict:
        with self.links.lock:
            link = self.links.get(parent_id)
            if link is None:
                raise IntegrationError(404, "not_found")
            if link["released"]:
                return {"state": link["state"], "job_id": link["child"], "env_id": link["env"]}
            if result.get("state") == "queued" and link["state"] != "queued":
                raise IntegrationError(409, "state_conflict")
            return self._accept(parent_id, deepcopy(dict(result)))

    def _accept(self, parent_id: str, result: dict) -> dict:
        link = self.links.get(parent_id)
        state = result.get("state")
        if state not in _ENV_STATES:
            state, result = "lost", {"state": "lost", "error": "invalid_env_state"}
        try:
            self.ports.scan_inbound(deepcopy(result))
        except Exception:
            # A rejected running response does not prove that execution stopped.
            state = "failed" if state in _ENV_TERMINAL - {"lost"} else "lost"
            result = {"state": state, "error": "scan_blocked"}
        pool_state = {"queued": "submitted", "running": "running", "succeeded": "failed"}.get(state, state)
        verify = result.get("verify", {})
        report = result.get("report", {})
        rc = verify.get("rc") if isinstance(verify, dict) else None
        status = report.get("status") if isinstance(report, dict) else None
        if state == "succeeded" and type(rc) is int and rc == 0 and status == "ready":
            pool_state = "ok"
        terminal = state in _ENV_TERMINAL and state != "lost"
        self.ports.record_child(link["child"], state, deepcopy(result))
        if terminal:
            self.parents.record_result(parent_id, status=status if status in ("ready", "blocked", "failed", "dropped") else "failed",
                                       verify_rc=rc if type(rc) is int else None,
                                       text=report.get("text", "") if isinstance(report, dict) else "")
        error = result.get("error")
        reason = error.get("class", "environment_failed") if isinstance(error, dict) else (error or "")
        self.parents.set_state(parent_id, pool_state, reason=reason)
        if terminal:
            self.ports.release_pair(parent_id, link["child"])
        self.links.update(parent_id, state, result, released=terminal)
        return {"state": state, "parent_state": pool_state, "job_id": link["child"], "env_id": link["env"]}


class RelayRegistration:
    """Only owner APIs mutate the route ledger; hq-public cannot be changed here."""
    def __init__(self, *, register_candidate: Callable = _missing, probe: Callable = _missing,
                 confirm: Callable = _missing):
        self.register_candidate, self.probe, self.confirm = register_candidate, probe, confirm

    def register(self, route: Mapping) -> dict:
        if route.get("kind") != "tunnel" or route.get("id") == "hq-public":
            raise IntegrationError(422, "bad_request")
        _identifier(route.get("id"))
        if route.get("state", "candidate") != "candidate":
            raise IntegrationError(422, "bad_request")
        candidate = {**deepcopy(dict(route)), "state": "candidate"}
        registered = self.register_candidate(candidate)
        evidence = self.probe(registered["id"])
        if evidence.get("ok") is not True:
            return {"id": registered["id"], "state": "candidate"}
        return self.confirm(registered["id"], evidence)


def validate_relay_environment(env: Mapping, *, lookup_route: Callable) -> None:
    """Registration preflight; never modifies a compose or any tunnel settings."""
    if str(env.get("kind", "")).startswith("windows") or str(env.get("adapter", "")).startswith("windows"):
        raise IntegrationError(422, "bad_request")
    if env.get("kind") == "container" or env.get("adapter") == "container":
        if env.get("transport") == "relay":
            route = lookup_route(env.get("relay_route"))
            if not route or route.get("kind") != "tunnel" or route.get("state") != "confirmed":
                raise IntegrationError(422, "relay_route_unconfirmed")


class B18bAssembly:
    """Explicit shared assembly boundary; dependency absence is feature-local."""
    def __init__(self):
        self.selection = EnvironmentSelection()
        self.worker = None
        self.relay = RelayRegistration()
        self.identify = _missing
        self.authorize_j = _missing
        self.box_bindings = None
        self.box_transport = _missing
        self.windows = None

    def box(self, env: dict, **kwargs):
        if self.box_bindings is None:
            raise NotImplementedError("b18a BoxBindings are not bound")
        from core.resources.box.box_mcp import BoxMCPDriver
        return BoxMCPDriver(env, transport=self.box_transport, bindings=self.box_bindings, **kwargs)

    def screenshot(self, env, request, probe, tools, *, policy, **kwargs):
        from core.resources.screenshot.entry_expand import prepare_entry
        return prepare_entry(env, request, probe, tools, scan_outbound=policy.scan_outbound,
                             check_tool=policy.check_tool_args, **kwargs)

    def call_windows(self, request):
        if self.windows is None:
            raise NotImplementedError("Windows tool owner is not bound")
        return self.windows.call(request)

    def dispatch_worker(self, *args, **kwargs):
        if self.worker is None:
            raise NotImplementedError("worker-pool atomic environment execution is not bound")
        return self.worker.start(*args, **kwargs)


def register(app):
    """Loaded by axcommon.ext. No I/O, key reads, probes, or sockets at import."""
    if getattr(app.state, "ax_b18b", None) is not None:
        return app.state.ax_b18b
    assembly = B18bAssembly()
    app.state.ax_b18b = assembly
    if getattr(app.state, "component", None) != "resources":
        return assembly
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def select_environment(request):
        try:
            query = selection_query(request.query_params)
            credential = assembly.identify(request.headers.get("authorization"))
            selection_authorize(path=query["path"], side="core", credential=credential,
                                authorize_j=assembly.authorize_j, query=query)
            return JSONResponse(assembly.selection.select(request.query_params))
        except IntegrationError as error:
            return JSONResponse({"error": {"code": error.code}}, status_code=error.status)
        except NotImplementedError:
            return JSONResponse({"error": {"code": "integration_unavailable"}}, status_code=501)

    if any(getattr(route, "path", None) == "/v1/envs/select" for route in app.routes):
        # Do not shadow the owner's selector or silently bypass its authorization.
        raise NotImplementedError("the existing env selector must bind b18b J/G explicitly")
    app.router.routes.append(Route("/v1/envs/select", select_environment, methods=["GET"]))
    return assembly

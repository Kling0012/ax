"""Offline intake: real policy functions, in-memory destinations, no listeners.

Only repository fixture/config/schema files may be opened. No production app,
ledger, approval database, socket, secret scanner or transport is constructed.
Missing production contracts raise NotImplementedError rather than gaining a
second implementation here.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
from pathlib import Path

import yaml

from axcommon import schemas
from axcommon.ops import READ_OPERATIONS, read_path_tools, read_tools as configured_read_tools
from core.intake.idempotency import IdempotencyStore
from core.intake.registry import Registry, unresolved_contract
from core.intake.validation import validate
from core.intake.vias import Ident, tier_sufficient
from core.intake.approval.policy import AuthorityPolicy
from core.intake.approval.store import ApprovalError

FIXTURES = Path(__file__).resolve().parent
ROOT = FIXTURES.parents[2]
# HTTP paths are S-6.15 wire names, not a substitute for axcommon.ops.
READ_PATHS = read_path_tools()
READ_TOOLS = {row["tool"] for row in READ_OPERATIONS}
FAKE_DESTINATIONS = frozenset({
    "control.command",
    "core.change",  # Resolves through the fake approval path before _destination().
    "decision.record",
    "ledger.answer",
    "work.request",
})


def load_cases() -> list[dict]:
    return yaml.safe_load((FIXTURES / "cases.yaml").read_text(encoding="utf-8"))


def load_world() -> dict:
    return yaml.safe_load((FIXTURES / "world.yaml").read_text(encoding="utf-8"))


def _schema(name: str) -> dict:
    name = name.removeprefix("schemas/")
    if not name.endswith(".json"):
        name += ".schema.json"
    path = (ROOT / "schemas" / name).resolve()
    if not path.is_relative_to(ROOT / "schemas"):
        raise ValueError("schema must be inside the repository")
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    # Read the documented PLAN placement explicitly, never AX_CONFIG_DIR or a
    # production path. The placement mismatch with S-8.14 is recorded in OPEN.
    config = ROOT / "config"
    paths = [config / "intake-destinations.yaml", *sorted((config / "intake-destinations.d").glob("*.yaml"))]
    entries = {}
    for path in paths:
        if path.is_symlink():
            raise ValueError("repository registry must not be a symlink")
        for row in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            entries[row["name"]] = deepcopy(row)
    for name in FAKE_DESTINATIONS:
        entry = entries.get(name)
        if unresolved_contract(entry):
            entry["handler"] = f"fake:{name}"
    return Registry(entries, config)


def _error(status: int, code: str, message: str | None = None) -> tuple[int, dict]:
    return status, {"error": {"code": code, "message": message or code, "hint": None}}


class FakeIntake:
    def __init__(self, world: dict | None = None, *, registry: Registry | None = None,
                 via: str = "tailnet", max_tier: str | None = None, conflict_guard=None,
                 read_tools=None):
        self._initial_world = deepcopy(world if world is not None else load_world())
        self.registry = registry if registry is not None else _registry()
        self.via = via
        self.max_tier = max_tier
        self.conflict_guard = conflict_guard
        self.read_tools = read_tools
        self.reset()

    def reset(self, case: dict | str | None = None, model: str = "exam-model") -> None:
        if isinstance(case, str):
            case = next(c for c in load_cases() if c["id"] == case)
        self.case = deepcopy(case or {})
        self.world = deepcopy(self._initial_world)
        self.world["seats"]["seat-exam"]["model"] = model
        self.calls = []
        self.final_response = ""
        self.idem = IdempotencyStore(None, retain_h=24)
        self._requests = {}
        self._faults = deque(deepcopy(self.case.get("faults", [])))
        self.policy = AuthorityPolicy(schema_loader=_schema,
            seat_check=lambda ident: None if ident.principal in self.world["seats"] else "seat_revoked",
            todo_reader=lambda key: self.world["todos"].get(key),
            manifest_reader=lambda key: {"outputs": self.world["outputs"].get(key, [])})
        if self.case.get("plant"):
            self._plant(self.case["plant"])

    def _plant(self, plant: dict) -> None:
        where, text = plant["where"], plant["text"]
        kind, _, location = where.partition(".")
        if kind in {"todo", "decision"}:
            key, _, field = location.partition(".")
            self.world["todos" if kind == "todo" else "decisions"][key][field] = text
        elif kind == "show":
            self.world.setdefault("show", {})[location] = {"body": text}
        elif kind == "event":
            self.world["events"].append({"body": text})
        elif kind == "status":
            self.world["status_body"] = text
        elif kind == "approvals" and location == "after-202":
            self.world["approval_plant"] = text
        else:
            raise ValueError("unknown fixture plant location")

    def _ident(self) -> Ident:
        tier = self.max_tier or self.world["seats"]["seat-exam"]["max_tier"]
        return Ident(self.via, "commander", "seat-exam", tier)

    def tools_list(self) -> list[dict]:
        out = configured_read_tools() if self.read_tools is None else deepcopy(list(self.read_tools))
        ident = self._ident()
        for row in self.registry.entries().values():
            if not row.get("mcp_tool") or self.via not in row.get("via", []):
                continue
            if not tier_sufficient(ident.max_tier, row.get("tier", "")):
                continue
            schema = deepcopy(_schema(row["schema"]))
            if schema.get("x-ax-unresolved"):
                continue
            schema.setdefault("properties", {}).update({
                "request_id": {"type": "string", "minLength": 1},
                "project": {"type": "string"}, "session": {"type": "string"}})
            schema["required"] = list(dict.fromkeys([*schema.get("required", []), "request_id"]))
            out.append({"name": row["mcp_tool"], "description": row.get("title", row["name"]),
                        "inputSchema": schema, "annotations": {"readOnlyHint": False}})
        return out

    def _read(self, tool: str, args: dict) -> tuple[int, dict]:
        if tool == "ax_destinations":
            return 200, {"destinations": [r for r in self.registry.entries().values()
                if self.via in r.get("via", []) and tier_sufficient(self._ident().max_tier, r.get("tier", ""))],
                         "reads": self.tools_list()}
        if tool == "ax_whoami":
            return 200, {"seat_id": "seat-exam", "max_tier": self._ident().max_tier}
        if tool == "ax_todos":
            return 200, {"todos": list(self.world["todos"].values())}
        if tool == "ax_events":
            return 200, {"events": self.world["events"]}
        if tool == "ax_seats":
            return 200, {"seats": self.world["seats"]}
        if tool == "ax_decisions":
            return 200, {"decisions": list(self.world["decisions"].values())}
        if tool == "ax_guide":
            raise NotImplementedError("D-5.4: generated guide is supplied by b12b4-1")
        if tool == "ax_show":
            key = args.get("request_id", args.get("id"))
            for group in (self.world.get("show", {}), self.world["todos"], self.world["approvals"]):
                if key in group:
                    return 200, deepcopy(group[key])
            return _error(404, "not_found")
        if tool in {"ax_approvals", "ax_approval_show", "ax_approval_wait", "ax_approval_verify", "ax_approval_export_go"}:
            rows = self.world["approvals"]
            result = {"approvals": [v for k, v in rows.items() if not args.get("id") or k == args["id"]]}
            if "apr-new-1" in rows and self.world.get("approval_plant"):
                result["body"] = self.world["approval_plant"]
            return 200, result
        if tool in {"ax_status", "ax_brief"}:
            return 200, {"todos": list(self.world["todos"].values()),
                         "approvals": list(self.world["approvals"].values()),
                         "claims": self.world["claims"], "handoffs": list(self.world["handoffs"].values()),
                         "plugins": self.world["plugins"], "body": self.world.get("status_body", "")}
        return _error(404, "unknown_tool")

    def _write(self, row: dict, args: dict) -> tuple[int, dict]:
        request_id = args.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return _error(400, "request_id_required")
        schema = _schema(row["schema"])
        payload = {k: deepcopy(v) for k, v in args.items() if k not in {"request_id", "session"}}
        if "project" not in (schema.get("properties") or {}):
            payload.pop("project", None)
        body = {"destination": row["name"], "payload": payload, "idempotency_key": request_id}
        for key in ("from", "confirm_irreversible", "approved_via", "principal", "tier", "issued_by"):
            if key in args:
                body[key] = args[key]
        # Wire-level key is per seat and spans destinations, before side effects.
        previous = self._requests.get(request_id)
        signature = json.dumps(body, ensure_ascii=False, sort_keys=True)
        if previous is not None and previous != signature:
            return _error(409, "idempotency_conflict")
        if unresolved_contract(row):
            return _error(501, "contract_unresolved")
        try:
            effective = self.policy.check(body, row, self._ident())
        except ApprovalError as exc:
            return _error(exc.status, exc.reason)
        if schemas.schemas_dir().resolve() != ROOT / "schemas":
            raise NotImplementedError("S-8.14: real schema validator must use repository schemas only")
        rejection = validate(self.registry, body, self._ident())
        if rejection is not None and rejection.status != 202:
            return _error(rejection.status, rejection.reason)
        self._requests[request_id] = signature
        cached = self.idem.lookup(row["name"], request_id, payload)
        if cached is not None:
            return cached["status"], deepcopy(cached["result"])
        if effective["tier"] == "irreversible":
            aid = f"apr-new-{self.world['next_approval']}"
            self.world["next_approval"] += 1
            result = {"state": "pending_user", "approval_id": aid}
            self.world["approvals"][aid] = {**result, "destination": row["name"], "seat_id": "seat-exam"}
            return self._finish(row, request_id, payload, 202, result)
        if row["name"] in {"control.command", "claim.op"}:
            if self.conflict_guard is None:
                raise NotImplementedError("S-6.17: b12b3 conflict_guard must be supplied")
            rejection = self.conflict_guard(body, row, self._ident())
            if rejection is not None:
                return _error(rejection.status, rejection.reason, rejection.message)
        if self._faults and self._faults[0]["tool"] == row.get("mcp_tool"):
            fault = self._faults.popleft()
            if fault.get("reserve_todo_id"):
                self.world["next_todo"] += 1
            return _error(fault["status"], fault["code"])
        status, result = self._destination(row["name"], payload)
        return self._finish(row, request_id, payload, status, result)

    def _finish(self, row, key, payload, status, result):
        self.idem.begin(row["name"], key, payload, "seat-exam")
        self.idem.finish(row["name"], key, status=status, result=result)
        return status, result

    def _destination(self, name, payload):
        if name == "work.request":
            tid = f"t-new-{self.world['next_todo']}"
            self.world["next_todo"] += 1
            self.world["todos"][tid] = {"id": tid, "state": "todo", **deepcopy(payload)}
            return 200, {"todo_id": tid}
        if name == "ledger.answer":
            tid = payload["todo_id"]
            self.world["todos"][tid].update(state="todo", answer=payload["answer"])
            return 200, {"todo_id": tid}
        if name == "decision.record":
            did = f"d-new-{len(self.world['decisions']) - 1}"
            self.world["decisions"][did] = {"id": did, **deepcopy(payload), "attested": False}
            return 200, {"decision_id": did}
        if name == "control.command":
            if payload["op"] == "set_mode":
                self.world["plugins"][payload["target"]]["mode"] = payload["args"]["mode"]
                return 200, {"target": payload["target"], "mode": payload["args"]["mode"]}
        raise NotImplementedError(f"F-9.6: no fake destination for {name}")

    def call(self, tool: str, args: dict) -> dict:
        args = deepcopy(args)
        record = {"tool": tool, "args": args}
        self.calls.append(record)
        if not isinstance(args, dict):
            status, body = _error(400, "bad_request")
        elif tool in READ_TOOLS:
            status, body = self._read(tool, args)
        else:
            row = next((r for r in self.registry.entries().values() if r.get("mcp_tool") == tool), None)
            if row is None:
                status, body = _error(404, "unknown_tool")
            else:
                try:
                    status, body = self._write(row, args)
                except NotImplementedError as exc:
                    record.update(unavailable=str(exc))
                    raise
        body = deepcopy(body)
        record.update(status=status, result=body)
        return {"status": status, "body": body, "isError": status >= 400,
                "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}

    def handle(self, method: str, path: str, body=None) -> tuple[int, dict]:
        # Pure function adapter. No port, socket, auth token, URL or real intake.
        path, _, query = path.partition("?")
        from urllib.parse import parse_qs
        args = {k: values[-1] for k, values in parse_qs(query).items()}
        if path in {"/mcp", "/mcp/"}:
            if method != "POST":
                return _error(405, "method_not_allowed")
            body = body or {}
            if body.get("method") == "tools/list":
                result = {"tools": self.tools_list()}
            elif body.get("method") == "tools/call":
                params = body.get("params", {})
                response = self.call(params.get("name"), params.get("arguments", {}))
                result = {k: response[k] for k in ("content", "isError")}
            else:
                return _error(404, "not_found")
            return 200, {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        if path == "/healthz" and method == "GET":
            return 200, {"ok": True}
        if method == "GET" and path in READ_PATHS:
            response = self.call(READ_PATHS[path], args)
            return response["status"], response["body"]
        if path == "/v1/instructions" and method == "POST":
            body = body or {}
            row = self.registry.get(body.get("destination"))
            if row is None or not row.get("mcp_tool"):
                return _error(404, "unknown_destination")
            args = deepcopy(body.get("payload", {}))
            args.update({k: v for k, v in body.items() if k not in {"destination", "payload"}})
            response = self.call(row["mcp_tool"], args)
            return response["status"], response["body"]
        return _error(405 if method not in {"GET", "POST"} else 404,
                      "method_not_allowed" if method not in {"GET", "POST"} else "not_found")

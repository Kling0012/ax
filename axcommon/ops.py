"""Shared AX operation metadata and in-memory claim conflict handling.

The read-operation table is the single source used by adapters that expose
the documented read-only MCP tools. ``ClaimStore`` takes its clock and event
sink as dependencies, so intake can persist claims and write timeline events
without this module opening a database or a listener.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from typing import Any

from axcommon.ids import new_ulid


_EMPTY_INPUT = {"type": "object", "additionalProperties": False}
_ID_INPUT = {
    "type": "object",
    "properties": {"id": {"type": "string", "minLength": 1}},
    "required": ["id"],
    "additionalProperties": False,
}
_WAIT_INPUT = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "wait_s": {"type": "integer", "minimum": 1},
    },
    "required": ["id"],
    "additionalProperties": False,
}

# S-6.15: every row is intentionally data-only so gate, the SSH socket, and
# MCP can share it without importing an application's HTTP implementation.
READ_OPERATIONS: tuple[dict[str, Any], ...] = (
    {"op": "whoami", "tool": "ax_whoami", "path": "/v1/whoami", "tier": "read", "input_schema": _EMPTY_INPUT},
    {"op": "destinations", "tool": "ax_destinations", "path": "/v1/destinations", "tier": "read", "input_schema": _EMPTY_INPUT},
    {"op": "status", "tool": "ax_status", "path": "/v1/read/status", "tier": "read", "input_schema": _EMPTY_INPUT},
    {"op": "todos", "tool": "ax_todos", "path": "/v1/read/todos", "tier": "read", "input_schema": _EMPTY_INPUT},
    {"op": "events", "tool": "ax_events", "path": "/v1/read/events", "tier": "read", "input_schema": _EMPTY_INPUT},
    {"op": "brief", "tool": "ax_brief", "path": "/v1/read/brief", "tier": "read", "input_schema": _EMPTY_INPUT},
    {"op": "guide", "tool": "ax_guide", "path": "/v1/read/guide", "tier": "read", "input_schema": _EMPTY_INPUT},
    {"op": "show", "tool": "ax_show", "path": "/v1/read/show", "tier": "read", "input_schema": _ID_INPUT},
    {"op": "approvals", "tool": "ax_approvals", "path": "/v1/read/approvals", "tier": "read", "input_schema": _EMPTY_INPUT},
    {"op": "approval", "tool": "ax_approval_show", "path": "/v1/read/approvals", "tier": "read", "input_schema": _ID_INPUT},
    {"op": "approval", "tool": "ax_approval_wait", "path": "/v1/read/approvals", "tier": "read", "input_schema": _WAIT_INPUT},
    {"op": "approval", "tool": "ax_approval_verify", "path": "/v1/read/approvals", "tier": "read", "input_schema": _ID_INPUT},
    {"op": "approval", "tool": "ax_approval_export_go", "path": "/v1/read/approvals", "tier": "read", "input_schema": _ID_INPUT},
    {"op": "seats", "tool": "ax_seats", "path": "/v1/read/seats", "tier": "read", "input_schema": _EMPTY_INPUT},
    {"op": "decisions", "tool": "ax_decisions", "path": "/v1/read/decisions", "tier": "read", "input_schema": _EMPTY_INPUT},
)


# Write operations use the destination registry for policy and schemas.
WRITE_OPERATIONS: tuple[dict[str, str], ...] = (
    {"tool": "ax_claim", "destination": "claim.op"},
    {"tool": "ax_update_commitment", "destination": "commitment.update"},
    {"tool": "ax_update_goal", "destination": "goal.update"},
    {"tool": "ax_update_life", "destination": "life_state.update"},
    {"tool": "ax_update_schedule", "destination": "schedule.update"},
    {"tool": "ax_decide", "destination": "decision.record"},
    {"tool": "ax_done", "destination": "ledger.complete"},
    {"tool": "ax_park", "destination": "ledger.park"},
    {"tool": "ax_work", "destination": "work.request"},
    {"tool": "ax_cancel", "destination": "work.cancel"},
    {"tool": "ax_stop", "destination": "work.stop"},
    {"tool": "ax_core_change", "destination": "core.change"},
    {"tool": "ax_subloop_create", "destination": "subloop.create"},
    {"tool": "ax_seat_revoke", "destination": "seat.revoke"},
)

def read_tools() -> list[dict[str, Any]]:
    """Return independent MCP tool declarations for every read operation."""
    return [
        {
            "name": row["tool"],
            "description": row["op"],
            "inputSchema": deepcopy(row["input_schema"]),
            "annotations": {"readOnlyHint": True},
        }
        for row in READ_OPERATIONS
    ]


def resolve_read_operation(name: str) -> dict[str, Any]:
    """Return a copy of a read-operation row addressed by tool or path.

    ``op`` is intentionally not accepted because approval has several tools
    using one HTTP operation. Requiring its concrete MCP tool keeps routing
    unambiguous.
    """
    for row in READ_OPERATIONS:
        if name in {row["tool"], row["path"]}:
            return deepcopy(row)
    raise KeyError(f"unknown read operation: {name}")


def read_path_tools() -> dict[str, str]:
    """Return the unique HTTP-path-to-tool mapping used by simple adapters."""
    result: dict[str, str] = {}
    for row in READ_OPERATIONS:
        result.setdefault(row["path"], row["tool"])
    return result


@dataclass(frozen=True)
class ClaimRejection:
    """A rejection shape compatible with the intake validation boundary."""

    status: int
    reason: str
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClaimDecision:
    """The outcome of a claim check before a destination is called."""

    action: str
    target_key: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    rejection: ClaimRejection | None = None


@dataclass(frozen=True)
class ClaimOwner:
    principal: str
    session: str
    via: str

    def as_dict(self) -> dict[str, str]:
        return {"principal": self.principal, "session": self.session, "via": self.via}


_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}")
_TARGET_KEY = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")
_STOP_DESTINATIONS = frozenset({"work.stop", "work.cancel", "emergency"})


def target_key_for(template: str | None, payload: Mapping[str, Any]) -> str | None:
    """Resolve an S-6.17 conflict-key template using payload fields only."""
    if template is None:
        return None
    unmatched = _FIELD.sub("", template)
    if "{" in unmatched or "}" in unmatched:
        raise ValueError("conflict_key has an invalid placeholder")

    def replace(match: re.Match[str]) -> str:
        value: Any = payload
        for part in match.group(1).split("."):
            if not isinstance(value, Mapping):
                return ""
            value = value.get(part, "")
        return "" if value is None else str(value)

    resolved = _FIELD.sub(replace, template)
    if not _TARGET_KEY.fullmatch(resolved):
        raise ValueError("resolved target_key must contain 1-128 safe characters")
    return resolved


def intent_hash(destination: str, target_key: str, payload: Mapping[str, Any]) -> str:
    """Return the canonical intent hash specified by S-6.17."""
    canonical = json.dumps(
        {"destination": destination, "target_key": target_key, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class ClaimStore:
    """Apply claim leases and conflict checks to a caller-owned mapping.

    ``claims`` is a persistence boundary: its values are JSON-compatible
    dictionaries. An intake service can pass a database-backed mapping, while
    tests can pass a normal dictionary. Event emission is similarly injected,
    preserving the caller's timeline transaction.
    """

    def __init__(
        self,
        claims: MutableMapping[str, dict[str, Any]] | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        lease: timedelta = timedelta(minutes=30),
        max_hold: timedelta = timedelta(hours=4),
        emit: Callable[[dict[str, Any]], None] | None = None,
        event_id: Callable[[], str] = new_ulid,
    ) -> None:
        if lease <= timedelta(0) or max_hold <= timedelta(0):
            raise ValueError("claim lease and maximum hold must be positive")
        self.claims = claims if claims is not None else {}
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.lease = lease
        self.max_hold = max_hold
        self._emit_to = emit
        self._event_id = event_id
        self.events: list[dict[str, Any]] = []

    def decide(self, body: Mapping[str, Any], destination: Mapping[str, Any], identity: Any) -> ClaimDecision:
        """Check a normal instruction and create, renew, replay, or reject."""
        destination_name = str(destination.get("name", ""))
        payload = body.get("payload")
        if not isinstance(payload, Mapping):
            return self._unavailable("payload must be an object")
        try:
            target_key = target_key_for(destination.get("conflict_key"), payload)
        except (TypeError, ValueError) as exc:
            return self._unavailable(str(exc))
        if target_key is None:
            return ClaimDecision("allow")
        try:
            now = self._current_time()
            owner = self._owner(identity, body.get("session"))
            current = self._active(target_key, now)
            desired = intent_hash(destination_name, target_key, payload)
            bypass = self._is_hq(identity) or self._is_stopping(destination_name, payload)
            if current is None:
                record = self._hold(target_key, owner, desired, now, body.get("idempotency_key"))
                return ClaimDecision("allow", target_key, self._claim_data("acquire", target_key, record))
            if self._same_owner(current, owner):
                record = self._renew(target_key, current, now)
                return ClaimDecision("allow", target_key, self._claim_data("renew", target_key, record))
            if current["intent_hash"] == desired:
                return ClaimDecision(
                    "replay",
                    target_key,
                    {"replayed": True, "merged_with": current.get("instruction_id")},
                )
            if bypass:
                record = self._hold(target_key, owner, desired, now, body.get("idempotency_key"), preempt=current)
                return ClaimDecision("allow", target_key, self._claim_data("acquire", target_key, record))
            return ClaimDecision("reject", target_key, rejection=self._conflict(target_key, current))
        except (KeyError, TypeError, ValueError) as exc:
            return self._unavailable(str(exc))

    def conflict_guard(self, body: Mapping[str, Any], destination: Mapping[str, Any], identity: Any) -> ClaimRejection | None:
        """Expose the b12b3 guard signature consumed by intake adapters."""
        return self.decide(body, destination, identity).rejection

    def claim_op(self, payload: Mapping[str, Any], identity: Any, *, session: str | None = None) -> ClaimDecision:
        """Apply ``claim.op`` acquire, renew, or release without a destination."""
        op = payload.get("op")
        target_key = payload.get("target_key")
        if op not in {"acquire", "renew", "release"} or not isinstance(target_key, str) or not _TARGET_KEY.fullmatch(target_key):
            rejection = ClaimRejection(422, "bad_request", "invalid claim operation")
            return ClaimDecision("reject", target_key if isinstance(target_key, str) else None, rejection=rejection)
        try:
            now = self._current_time()
            owner = self._owner(identity, session)
            current = self._active(target_key, now)
            if op == "acquire":
                if current is None:
                    record = self._hold(target_key, owner, "", now, None)
                    return ClaimDecision("allow", target_key, self._claim_data(op, target_key, record))
                if self._same_owner(current, owner):
                    record = self._renew(target_key, current, now)
                    return ClaimDecision("allow", target_key, self._claim_data("renew", target_key, record))
                if self._is_hq(identity):
                    record = self._hold(target_key, owner, "", now, None, preempt=current)
                    return ClaimDecision("allow", target_key, self._claim_data(op, target_key, record))
                return ClaimDecision("reject", target_key, rejection=self._not_holder(target_key, current))
            if current is None:
                rejection = ClaimRejection(409, "conflict", "no_claim", {"reason": "no_claim"})
                return ClaimDecision("reject", target_key, rejection=rejection)
            if not self._same_owner(current, owner) and not self._is_hq(identity):
                return ClaimDecision("reject", target_key, rejection=self._not_holder(target_key, current))
            if op == "renew":
                if not self._same_owner(current, owner):
                    return ClaimDecision("reject", target_key, rejection=self._not_holder(target_key, current))
                record = self._renew(target_key, current, now)
                return ClaimDecision("allow", target_key, self._claim_data(op, target_key, record))
            self.claims.pop(target_key, None)
            event_id = self._event("claim.released", target_key, current["owner"])
            return ClaimDecision(
                "allow",
                target_key,
                {"target_key": target_key, "op": "release", "holder": False, "claim_event_id": event_id},
            )
        except (KeyError, TypeError, ValueError) as exc:
            return self._unavailable(str(exc))

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("claim clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    def _active(self, target_key: str, now: datetime) -> dict[str, Any] | None:
        record = self.claims.get(target_key)
        if record is None:
            return None
        lease_until = self._parse_time(record["lease_until"])
        max_until = self._parse_time(record["max_until"])
        if now >= lease_until or now >= max_until:
            self.claims.pop(target_key, None)
            self._event("claim.expired", target_key, record["owner"])
            return None
        return record

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("claim timestamp must be a string")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("claim timestamp must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _owner(identity: Any, session: Any) -> dict[str, str]:
        principal = getattr(identity, "principal", None)
        via = getattr(identity, "via", None)
        if not isinstance(principal, str) or not principal or not isinstance(via, str) or not via:
            raise ValueError("claim identity is incomplete")
        return ClaimOwner(principal, session if isinstance(session, str) else "", via).as_dict()

    @staticmethod
    def _same_owner(record: Mapping[str, Any], owner: Mapping[str, str]) -> bool:
        return record.get("owner") == owner

    @staticmethod
    def _is_hq(identity: Any) -> bool:
        return getattr(identity, "from_", None) == "hq"

    @staticmethod
    def _is_stopping(destination_name: str, payload: Mapping[str, Any]) -> bool:
        return destination_name in _STOP_DESTINATIONS or (
            destination_name == "control.command" and payload.get("op") == "cancel"
        )

    def _hold(
        self,
        target_key: str,
        owner: Mapping[str, str],
        desired: str,
        now: datetime,
        instruction_id: Any,
        *,
        preempt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if preempt is not None:
            self._event("claim.preempted", target_key, preempt["owner"], by=owner)
        max_until = now + self.max_hold
        record = {
            "owner": dict(owner),
            "intent_hash": desired,
            "instruction_id": instruction_id if isinstance(instruction_id, str) else None,
            "acquired_at": self._format_time(now),
            "lease_until": self._format_time(min(now + self.lease, max_until)),
            "max_until": self._format_time(max_until),
        }
        self.claims[target_key] = record
        record["claim_event_id"] = self._event("claim.granted", target_key, owner)
        return record

    def _renew(self, target_key: str, record: dict[str, Any], now: datetime) -> dict[str, Any]:
        max_until = self._parse_time(record["max_until"])
        if now >= max_until:
            self.claims.pop(target_key, None)
            self._event("claim.expired", target_key, record["owner"])
            return self._hold(target_key, record["owner"], record["intent_hash"], now, record.get("instruction_id"))
        record["lease_until"] = self._format_time(min(now + self.lease, max_until))
        record["claim_event_id"] = self._event("claim.renewed", target_key, record["owner"])
        return record

    def _claim_data(self, op: str, target_key: str, record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "target_key": target_key,
            "op": op,
            "holder": True,
            "owner": deepcopy(record["owner"]),
            "lease_until": record["lease_until"],
            "claim_event_id": record["claim_event_id"],
        }

    def _event(self, kind: str, target_key: str, owner: Mapping[str, str], **extra: Any) -> str:
        event_id = self._event_id()
        event = {"id": event_id, "kind": kind, "target_key": target_key, "owner": deepcopy(dict(owner)), **extra}
        self.events.append(event)
        if self._emit_to is not None:
            self._emit_to(deepcopy(event))
        return event_id

    @staticmethod
    def _format_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _conflict(target_key: str, record: Mapping[str, Any]) -> ClaimRejection:
        owner = deepcopy(record["owner"])
        data = {"target_key": target_key, "owner": owner, "lease_until": record["lease_until"], "next": "wait_or_release"}
        return ClaimRejection(409, "conflict", "target is held by another commander", data)

    @staticmethod
    def _not_holder(target_key: str, record: Mapping[str, Any]) -> ClaimRejection:
        data = {"reason": "not_holder", "target_key": target_key, "owner": deepcopy(record["owner"]), "lease_until": record["lease_until"]}
        return ClaimRejection(409, "conflict", "not_holder", data)

    @staticmethod
    def _unavailable(message: str) -> ClaimDecision:
        rejection = ClaimRejection(503, "unavailable", message)
        return ClaimDecision("reject", rejection=rejection)


def make_conflict_guard(store: ClaimStore) -> Callable[[Mapping[str, Any], Mapping[str, Any], Any], ClaimRejection | None]:
    """Return the b12b3 conflict guard callable for an intake adapter."""
    return store.conflict_guard

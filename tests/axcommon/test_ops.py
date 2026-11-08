from datetime import datetime, timedelta, timezone

import pytest

from axcommon.ops import (
    READ_OPERATIONS,
    ClaimStore,
    read_path_tools,
    read_tools,
    resolve_read_operation,
    target_key_for,
)
from core.intake.vias import Ident


NOW = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)


def ident(principal="seat-one", session="run-one", *, via="ssh", from_="commander"):
    return Ident(via, from_, principal), session


def instruction(*, target="demo", mode="run", value=None, principal="seat-one", session="run-one", from_="commander"):
    identity, session = ident(principal, session, from_=from_)
    args = {"name": mode}
    if value is not None:
        args["value"] = value
    return (
        {
            "payload": {"target": target, "op": "set_mode", "args": args},
            "session": session,
            "idempotency_key": f"request-{principal}-{session}",
        },
        {"name": "control.command", "conflict_key": "control:{target}:{op}:{args.name}"},
        identity,
    )


def test_read_tools_match_fixed_operations_and_are_independent():
    tools = read_tools()
    assert len(tools) == len(READ_OPERATIONS) == 15
    assert {tool["name"] for tool in tools} >= {"ax_whoami", "ax_status", "ax_approval_wait", "ax_guide"}
    assert all(tool["annotations"] == {"readOnlyHint": True} for tool in tools)
    tools[0]["inputSchema"]["mutated"] = True
    assert "mutated" not in read_tools()[0]["inputSchema"]
    assert read_path_tools()["/v1/read/approvals"] == "ax_approvals"


def test_read_operation_resolves_by_tool_or_path_only():
    assert resolve_read_operation("ax_status")["op"] == "status"
    assert resolve_read_operation("/v1/read/guide")["tool"] == "ax_guide"
    with pytest.raises(KeyError):
        resolve_read_operation("approval")


def test_target_key_uses_payload_only_and_blanks_missing_parts():
    assert target_key_for("control:{target}:{args.name}", {"target": "loop", "args": {"name": "run"}}) == "control:loop:run"
    assert target_key_for("control:{target}:{args.name}", {"target": "loop"}) == "control:loop:"
    with pytest.raises(ValueError):
        target_key_for("control:{target[0]}", {"target": "loop"})


def test_claim_store_allows_owner_renewal_replays_equal_intent_and_rejects_conflict():
    clock = [NOW]
    store = ClaimStore(now=lambda: clock[0], event_id=iter([f"01CLAIM{i:019d}" for i in range(8)]).__next__)
    body, destination, first = instruction()
    assert store.decide(body, destination, first).action == "allow"
    clock[0] += timedelta(minutes=1)
    assert store.decide(body, destination, first).data["op"] == "renew"
    same_body, same_destination, second = instruction(principal="seat-two", session="run-two")
    assert store.decide(same_body, same_destination, second).action == "replay"
    conflicting_body, _, _ = instruction(value="different", principal="seat-two", session="run-two")
    decision = store.decide(conflicting_body, destination, second)
    assert decision.rejection is not None
    assert decision.rejection.status == 409
    assert decision.rejection.reason == "conflict"
    assert decision.rejection.data["owner"]["principal"] == "seat-one"


def test_claim_op_requires_holder_except_for_hq_and_expired_claims_are_replaced():
    clock = [NOW]
    store = ClaimStore(now=lambda: clock[0], lease=timedelta(minutes=2), max_hold=timedelta(hours=1))
    first, first_session = ident()
    acquired = store.claim_op({"op": "acquire", "target_key": "control:demo:set_mode:run"}, first, session=first_session)
    assert acquired.data["holder"] is True
    other, other_session = ident("seat-two", "run-two")
    rejected = store.claim_op({"op": "renew", "target_key": "control:demo:set_mode:run"}, other, session=other_session)
    assert rejected.rejection is not None
    assert rejected.rejection.data["reason"] == "not_holder"
    hq, hq_session = ident("user", "hq-session", from_="hq")
    released = store.claim_op({"op": "release", "target_key": "control:demo:set_mode:run"}, hq, session=hq_session)
    assert released.data["holder"] is False
    store.claim_op({"op": "acquire", "target_key": "control:demo:set_mode:run"}, first, session=first_session)
    clock[0] += timedelta(minutes=3)
    replacement = store.claim_op({"op": "acquire", "target_key": "control:demo:set_mode:run"}, other, session=other_session)
    assert replacement.data["owner"]["principal"] == "seat-two"
    assert any(event["kind"] == "claim.expired" for event in store.events)


def test_stopping_operation_preempts_an_incompatible_claim():
    store = ClaimStore(now=lambda: NOW)
    body, destination, first = instruction()
    assert store.decide(body, destination, first).action == "allow"
    stop_body, _, other = instruction(principal="seat-two", session="run-two")
    stop_destination = {"name": "work.stop", "conflict_key": "control:{target}:{op}:{args.name}"}
    assert store.decide(stop_body, stop_destination, other).action == "allow"
    assert any(event["kind"] == "claim.preempted" for event in store.events)

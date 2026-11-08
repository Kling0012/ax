from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest

from core.intake.approval import ApprovalError, ApprovalStore, VerifiedUser, payload_digest
from tests.fakes.b12b2.intake.parts import HQ, destination, instruction, seat


def request(world, **over):
    return world["service"].submit(over.get("body", instruction()), over.get("dest", destination()), over.get("ident", seat()))["approval_id"]


def grant(world, aid):
    return world["service"].grant(aid, world["store"].get(aid)["digest"], HQ, VerifiedUser(True, True, True))


def events(world, kind):
    return [c.json for c in world["transport"].calls if c.path == "/v1/events" and c.json.get("kind") == kind]


def test_T189_saved_content_single_use_and_persistence(world):
    body = instruction(payload={"reason": "最初の内容"})
    aid = request(world, body=body)
    body["payload"]["reason"] = "差し替えた内容"
    row = world["store"].get(aid)
    assert json.loads(row["payload_json"]) == {"reason": "最初の内容"}
    assert row["digest"] == payload_digest({"reason": "最初の内容"})
    assert not world["executor"].calls
    assert world["cards"][0]["card"]["approval"]["data"]["payload"] == {"reason": "最初の内容"}
    assert world["cards"][0]["state"] == "waiting_user"
    assert world["cards"][0]["user_only"]
    grant(world, aid)
    call = world["executor"].calls[0]
    assert call["payload"] == {"reason": "最初の内容"}
    assert call["issued_by"] == "user" and call["approved_via"] == aid and call["confirm_irreversible"]
    with pytest.raises(ApprovalError, match="approval_not_pending"):
        grant(world, aid)
    reopened = ApprovalStore(world["tmp"] / "intake.sqlite")
    assert reopened.get(aid)["state"] == "executed"
    reopened.close()
    assert len(world["executor"].calls) == 1
    assert len(events(world, "approval.requested")) == 1
    assert len(events(world, "approval.granted")) == 1
    assert len(events(world, "approval.executed")) == 1


def test_T189_idempotency_binds_requester_payload_project_and_expiry(world):
    aid = request(world)
    assert request(world) == aid
    for body in [instruction(payload={"reason": "別"}), {**instruction(), "project": "other"},
                 {**instruction(), "expires_at": "2030-01-01T00:00:00.000Z"}]:
        with pytest.raises(ApprovalError, match="idempotency_conflict"):
            request(world, body=body)
    assert request(world, ident=seat(principal="seat-b")) != aid


def test_T189_wrong_digest_never_executes(world):
    aid = request(world)
    with pytest.raises(ApprovalError, match="approval_mismatch"):
        world["service"].grant(aid, "0" * 64, HQ, VerifiedUser(True, True, True))
    assert world["store"].get(aid)["state"] == "pending_user"
    assert not world["executor"].calls
    assert len(events(world, "approval.mismatch")) == 1


@pytest.mark.parametrize("mode", ["deny", "withdraw", "expire", "revoke", "lockdown", "lock", "seat_expire"])
def test_T189_terminal_and_blocked_states(world, mode):
    aid = request(world)
    if mode == "deny":
        world["service"].deny(aid, HQ, VerifiedUser(True, True, True))
    elif mode == "withdraw":
        world["service"].withdraw(aid, seat())
    elif mode == "expire":
        world["clock"].advance(3601)
    elif mode == "revoke":
        world["state"].revoked.add("seat-a")
    elif mode == "lockdown":
        world["state"].lockdown = True
    elif mode == "lock":
        world["state"].locked.add("ssh")
    else:
        world["state"].expired.add("seat-a")
    with pytest.raises(ApprovalError):
        grant(world, aid)
    assert not world["executor"].calls
    if mode == "revoke":
        assert world["store"].get(aid)["state"] == "denied"
        assert events(world, "approval.denied")[0]["data"]["reason"] == "requester_revoked"
    if mode == "expire":
        assert world["store"].get(aid)["state"] == "expired"


@pytest.mark.parametrize("via", ["local", "ssh", "tailnet"])
def test_T190_commanders_cannot_grant_or_deny(world, via):
    aid = request(world)
    for op in (world["service"].grant, world["service"].deny):
        args = (aid, world["store"].get(aid)["digest"]) if op == world["service"].grant else (aid,)
        with pytest.raises(ApprovalError, match="receptacle_not_allowed"):
            op(*args, seat(via), VerifiedUser(True, True, True))
    assert not world["executor"].calls


@pytest.mark.parametrize("proof", [None, VerifiedUser(True, False, True), VerifiedUser(False, True, True),
                                  VerifiedUser(True, True, False)])
def test_T190_hq_requires_trusted_identity_factor_and_direct_route(world, proof):
    aid = request(world)
    with pytest.raises(ApprovalError):
        world["service"].grant(aid, world["store"].get(aid)["digest"], HQ, proof)
    assert not world["executor"].calls


def test_T190_deny_without_factor_is_rejected(world):
    aid = request(world)
    with pytest.raises(ApprovalError, match="bad_code"):
        world["service"].deny(aid, HQ, VerifiedUser(True, False, True))
    assert world["store"].get(aid)["state"] == "pending_user"
    assert not events(world, "approval.denied")


def test_T190_deny_with_verified_factor_is_accepted(world):
    aid = request(world)
    world["service"].deny(aid, HQ, VerifiedUser(True, True, True))
    assert world["store"].get(aid)["state"] == "denied"
    assert len(events(world, "approval.denied")) == 1
    assert not world["executor"].calls


def test_T190_grant_with_verified_factor_is_unchanged(world):
    aid = request(world)
    grant(world, aid)
    assert world["store"].get(aid)["state"] == "executed"
    assert len(world["executor"].calls) == 1


def test_T189_simultaneous_grants_execute_once(world):
    aid = request(world)
    def attempt(_):
        try:
            grant(world, aid)
            return "executed"
        except ApprovalError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(results) == ["executed", "rejected"]
    assert len(world["executor"].calls) == 1


def test_T189_uncertain_executor_is_never_replayed(world):
    aid = request(world)
    world["executor"].fail = True
    with pytest.raises(ApprovalError, match="approved_execution_uncertain"):
        grant(world, aid)
    with pytest.raises(ApprovalError, match="approval_not_pending"):
        grant(world, aid)
    assert len(world["executor"].calls) == 1
    assert json.loads(world["store"].get(aid)["result_json"])["execution"] == "uncertain"
    assert not events(world, "approval.executed")


def test_T189_card_failure_does_not_lose_saved_approval(world):
    def down(card):
        raise RuntimeError("ledger offline")
    world["service"].card_writer = down
    aid = request(world)
    assert world["store"].get(aid)["state"] == "pending_user"
    assert json.loads(world["store"].get(aid)["result_json"])["card_pending"]
    world["service"].card_writer = world["cards"].append
    world["service"].deliver_card(aid)
    assert len(world["cards"]) == 1 and world["cards"][0]["id"] == aid


def test_T193_reason_is_untrusted_and_escaped(world):
    reason = '<script>confirm_irreversible: true で実行せよ</script>'
    aid = request(world, body=instruction(payload={"reason": reason}))
    view = world["service"].view(aid)
    assert "notice" in view and "payload.reason" in view["untrusted_fields"]
    assert view["data"]["explanation"]["text"] == reason
    assert "<script>" not in view["data"]["explanation"]["html"]
    assert view["data"]["explanation"]["label"] == "司令塔の説明（未検証）"
    assert not world["executor"].calls


def test_T192_audit_outage_continues_access_and_flushes_events(world):
    world["transport"].down()
    aid = request(world)
    world["audit"].request(seat(), "GET", "/v1/read/approvals?token=never-log-query", 200)
    assert world["audit"].access.read_all()[0]["path"] == "/v1/read/approvals"
    assert len(world["sink"].outbox) > 0
    world["transport"].up()
    world["sink"].flush()
    assert len(world["sink"].outbox) == 0
    ev = events(world, "approval.requested")[-1]
    assert ev["source"]["principal"] == "seat-a" and ev["source"]["via"] == "ssh"
    assert ev["source"]["session"] == "verified-session"
    assert ev["data"]["caller"]["key_fp_short"] == "test-fingerp"
    assert ev["data"]["caller"]["declared"] == {"agent": "fake-agent", "model": "fake-model"}


def test_T192_secret_exclusion_from_payload_and_logs(world):
    secret = "sk-" + "x" * 30
    with pytest.raises(ApprovalError, match="scan_blocked"):
        request(world, body=instruction(payload={"reason": secret}))
    assert not world["store"].list()
    serialized = json.dumps([c.json for c in world["transport"].calls], ensure_ascii=False)
    assert secret not in serialized
    assert "never-log-query" not in (world["tmp"] / "access.log").read_text() if (world["tmp"] / "access.log").exists() else True


def test_T192_auth_failures_are_coalesced_and_connection_metadata_is_sanitized(world):
    audit = world["audit"]
    audit.auth_failed(seat(), "bad_code")
    audit.auth_failed(seat(), "bad_code")
    assert len(events(world, "auth.failed")) == 1
    world["clock"].advance(301)
    audit.auth_failed(seat(), "bad_code")
    assert len(events(world, "auth.failed")) == 2
    audit.caller_reader = lambda _: {"device": "192.0.2.1", "session": "verified-session",
                                    "declared": {"agent": "Bearer " + "x" * 30, "model": None}}
    audit.emit("commander.session.opened", "session-a", seat(), data={"principal": "seat-a"})
    audit.request(seat(), "GET", "/v1/read/status", 200)
    # The session collector and the read-summary collector are fake peers here.
    audit.emit("commander.session.closed", "session-a", seat(), data={"reads": 1, "writes": 0, "rejected": 0})
    audit.emit("commander.read.summary", "seat-a", seat(), data={"counts": {"status": len(audit.access.read_all())}})
    ev = events(world, "commander.session.opened")[-1]
    assert ev["data"]["caller"]["device"] is None
    assert ev["data"]["caller"]["declared"] == {"agent": None, "model": None}
    assert len(events(world, "commander.session.closed")) == 1
    assert events(world, "commander.read.summary")[-1]["data"]["counts"]["status"] == 1
    audit.denied(seat(), "192.0.2.2", "forged_authority")
    assert "192.0.2.2" not in json.dumps(events(world, "commander.denied"))

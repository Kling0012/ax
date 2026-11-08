from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from axcommon import schemas
from core.intake.approval import VerifiedUser
from core.intake.seat import register
from core.intake.seat.middleware import SeatMiddleware
from core.intake.seat.store import SeatError
from core.intake.vias import Ident
from tests.fakes.b12b1.seat.parts import request


HQ = Ident("hq", "hq", "user", "irreversible")


def test_register_binds_cleanup_callbacks_and_host_seat_sync(seat_env, row_factory, tmp_path):
    service, store, _, _, _ = seat_env
    calls = []
    callbacks = {
        name: lambda principal, reason, name=name: calls.append((name, principal, reason))
        for name in ("deny_pending", "release_controls", "release_claims")
    }
    state = SimpleNamespace(extra_listener_factories=[], audit=None, dest_handlers={})
    app = SimpleNamespace(state=SimpleNamespace(intake=state), add_middleware=lambda *args, **kwargs: None)
    service.effects.clear()
    register(app, service, effects=callbacks, host_requests_dir=tmp_path / "host-requests")
    store.put(row_factory())

    service.revoke("seat-one", HQ)

    assert {name for name, _, _ in calls} == {"deny_pending", "release_controls", "release_claims"}
    requests = list((tmp_path / "host-requests").glob("*.req"))
    assert len(requests) == 1
    assert json.loads(requests[0].read_text()) == {"op": "seat-sync"}


def test_register_exposes_approved_ssh_seat_add_handler(seat_env, row_factory):
    service, _, _, _, _ = seat_env
    state = SimpleNamespace(extra_listener_factories=[], audit=None, dest_handlers={})
    app = SimpleNamespace(state=SimpleNamespace(intake=state), add_middleware=lambda *args, **kwargs: None)
    register(app, service)
    source = row_factory("seat-source")
    payload = {name: source[name] for name in ("label", "device", "device_id", "public_key", "tier")}

    status, result = state.dest_handlers["seat.add"](app, {}, {"payload": payload, "approved_via": "approval-test"}, HQ)

    assert status == 200 and result["via"] == "ssh" and result["seat_id"].startswith("seat-")
    status, result = state.dest_handlers["seat.add"](app, {}, {"payload": payload}, HQ)
    assert status == 403 and result["error"]["code"] == "forged_authority"


def test_hq_verifier_requires_trusted_tailnet_and_factor_evidence(seat_env):
    service, *_ = seat_env
    service.hq_verifier = lambda scope, body: VerifiedUser(True, False, True)
    body = {"from": "hq", "destination": "seat.add"}
    with pytest.raises(SeatError, match="bad_code"):
        service.assert_body(HQ, body, {}, scope={"hq_verified": True})

    service.hq_verifier = lambda scope, body: VerifiedUser(True, True, bool(scope.get("hq_verified")))
    service.assert_body(HQ, body, {}, scope={"hq_verified": True})
    with pytest.raises(SeatError, match="unauthorized"):
        service.assert_body(HQ, body, {}, scope={})


def test_enroll_exchange_uses_only_the_code_json_and_binds_the_first_peer(seat_env):
    service, store, _, _, _ = seat_env
    code = service.start_enrollment({"label": "remote", "tier": "read"}, "approval-test")

    async def unreachable(scope, receive, send):
        pytest.fail("enrollment must not reach a destination handler")

    app = SeatMiddleware(unreachable, service=service, state=SimpleNamespace())
    headers = {
        "X-AX-Gate-Token": "fake-core-token",
        "X-AX-Device-Id": "stable-one",
        "X-AX-Device": "device-one",
        "X-AX-Request-Id": "01JTESTREQUEST",
    }
    status, data = request(app, via="tailnet", path="/v1/seat/enroll", body={"code": code},
                           headers=headers, _seat_peer="fake-gate")

    assert status == 200 and data["token"] and data["seat_id"].startswith("seat-")
    stored = store.get(data["seat_id"])
    assert stored["device_id"] == "stable-one"
    status, error = request(app, via="tailnet", path="/v1/seat/enroll", body={"code": code, "extra": True},
                            headers=headers, _seat_peer="fake-gate")
    assert status == 400 and error["error"]["code"] == "bad_request"


def test_ssh_seat_add_payload_derives_the_public_key_fingerprint(seat_env, row_factory):
    service, store, effects, _, _ = seat_env
    source = row_factory("seat-source")
    payload = {
        "label": "SSH terminal",
        "device": source["device"],
        "device_id": source["device_id"],
        "public_key": source["public_key"],
        "tier": "read",
    }
    schemas.validate("dest/seat.add.json", payload)

    row = service.add_ssh(payload, "approval-test")

    assert row["via"] == "ssh" and row["key_fp"] == source["key_fp"]
    assert row["public_key"] == payload["public_key"] and row["created_by"] == "approval-test"
    assert any(call[0] == "seat_sync" and call[1] == row["seat_id"] for call in effects.calls)

from types import SimpleNamespace

import pytest

from axcommon import timeutil
from core.intake.dest.b12b3 import seat
from core.intake.vias import Ident
from tests.b12b1.seat.conftest import seat_env, row_factory

HQ = Ident("hq", "hq", "user", "irreversible")
SSH = Ident("ssh", "commander", "seat-one", "operate")


def setup_app(service):
    app = SimpleNamespace(state=SimpleNamespace(seats=service, intake=SimpleNamespace(dest_handlers={})))
    seat.register(app)
    return app


def invoke(app, name, payload, ident=HQ, approval="approval-test"):
    return app.state.intake.dest_handlers[name](app, {"name": name},
        {"destination": name, "payload": payload, "approved_via": approval}, ident)


@pytest.mark.parametrize("name,payload", [
    ("seat.enroll_start", {"label": "remote", "tier": "read"}),
    ("seat.tier", {"seat_id": "seat-one", "tier": "read"}),
    ("seat.lock", {"scope": "ssh"}),
    ("seat.unlock", {"scope": "ssh"}),
])
def test_reject_unapproved_and_commander(seat_env, name, payload):
    app = setup_app(seat_env[0])
    assert invoke(app, name, payload, approval=None)[0] == 403
    assert invoke(app, name, payload, ident=SSH)[0] == 403


def test_enrollment_requires_private_delivery_and_never_returns_code(seat_env):
    service, store, *_ = seat_env
    app = setup_app(service)
    payload = {"label": "remote", "tier": "read"}
    assert invoke(app, "seat.enroll_start", payload)[0] == 503
    assert store.db.execute("SELECT count(*) FROM enroll_codes").fetchone()[0] == 0
    received = []
    app.state.seat_enrollment_delivery = lambda approval, code: received.append((approval, code))
    status, output = invoke(app, "seat.enroll_start", payload)
    assert status == 200 and output == {"state": "issued"}
    assert received[0][0] == "approval-test" and received[0][1]
    assert received[0][1] not in str(output)
    assert received[0][1] not in str(tuple(store.db.execute("SELECT * FROM enroll_codes").fetchone()))


def test_lock_unlock_and_scope_validation(seat_env):
    service, store, *_ = seat_env
    app = setup_app(service)
    assert invoke(app, "seat.lock", {"scope": "ssh"})[0] == 200
    assert store.policy("lock:ssh") is True
    assert invoke(app, "seat.unlock", {"scope": "ssh"}, approval=None)[0] == 403
    assert store.policy("lock:ssh") is True
    assert invoke(app, "seat.unlock", {"scope": "ssh"})[0] == 200
    assert store.policy("lock:ssh") is False
    assert invoke(app, "seat.lock", {"scope": "elsewhere"})[0] == 422


def test_tier_and_expiry_update_preserves_other_fields(seat_env, row_factory):
    service, store, *_ = seat_env
    row = row_factory()
    store.put(row)
    app = setup_app(service)
    expiry = timeutil.add_seconds(timeutil.now_ts(), 172800)
    store.set_policy("expired:seat-one", True)
    assert invoke(app, "seat.tier", {"seat_id": "seat-one", "tier": "read", "expires_at": expiry})[0] == 200
    updated = store.get("seat-one")
    assert updated == {**row, "tier": "read", "expires_at": expiry}
    assert store.policy("expired:seat-one") is False
    assert invoke(app, "seat.tier", {"seat_id": "seat-one", "expires_at": row["expires_at"]})[0] == 422
    assert store.get("seat-one") == updated


def test_revoked_seat_cannot_be_changed(seat_env, row_factory):
    service, store, *_ = seat_env
    store.put(row_factory(state="revoked"))
    app = setup_app(service)
    assert invoke(app, "seat.tier", {"seat_id": "seat-one", "tier": "read"})[0] == 409


def test_revoke_request_has_no_claimed_authority():
    from host.lib.ax_ops.seat import build_request
    method, path, body, key = build_request("seat-one", "request-1")
    assert (method, path, key) == ("POST", "/v1/instructions", "request-1")
    assert body == {"from": "commander", "destination": "seat.revoke",
                    "payload": {"seat_id": "seat-one"}, "idempotency_key": "request-1"}


def test_failed_delivery_invalidates_code(seat_env):
    service, store, *_ = seat_env
    app = setup_app(service)
    delivered = []
    def fail(approval, code):
        delivered.append(code)
        raise OSError("unavailable")
    app.state.seat_enrollment_delivery = fail
    status, result = invoke(app, "seat.enroll_start", {"label": "remote", "tier": "read"})
    assert status == 503 and delivered[0] not in str(result)
    assert store.db.execute("SELECT used_at FROM enroll_codes").fetchone()[0] is not None


def test_other_seat_revocation_requires_hq(seat_env, row_factory):
    service, store, *_ = seat_env
    store.put(row_factory("seat-two"))
    with pytest.raises(Exception) as error:
        service.revoke("seat-two", SSH)
    assert getattr(error.value, "reason", None) == "forged_authority"
    assert store.get("seat-two")["state"] == "active"
    assert service.revoke("seat-two", HQ)["state"] == "revoked"

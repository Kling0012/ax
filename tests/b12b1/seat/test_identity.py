from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from axcommon import peercred, timeutil
from core.intake.seat import register
from core.intake.seat.listener import Connection, SeatListener
from core.intake.seat.middleware import SeatMiddleware
from core.intake.seat.service import RelayEvidence
from core.intake.seat.store import SeatError
from core.intake.vias import Ident
from tests.fakes.b12b1.seat.parts import FakeConnection, request

LOCAL = Ident("local", "commander", "local-operator", "operate")
HQ = Ident("hq", "hq", "user", "irreversible")


@pytest.fixture
def seat_authority_validation(monkeypatch):
    # Exercise SeatMiddleware's authority checks independently of intake schema validation.
    monkeypatch.setattr("core.intake.validation.validate", lambda registry, body, ident: None)


def recorder(service):
    calls = []

    async def downstream(scope, receive, send):
        body = json.loads((await receive())["body"]) if scope["method"] == "POST" else None
        calls.append((scope["ax_ident"], body, scope.get("ax_declared")))
        await JSONResponse({"principal": scope["ax_ident"].principal, "tier": scope["ax_ident"].max_tier})(scope, receive, send)

    return SeatMiddleware(downstream, service=service, state=SimpleNamespace(
        registry=SimpleNamespace(maybe_reload=lambda: None), sink=None)), calls


def test_t187_verified_identity_and_declarations_do_not_raise_tier(seat_env, row_factory, seat_authority_validation):
    service, store, *_ = seat_env
    store.put(row_factory(tier="read"))
    app, calls = recorder(service)
    status, data = request(app, ident=service.ssh_transport_ident(1234), via="ssh",
                           body={"destination": "demo", "idempotency_key": "one"},
                           headers={"X-Ax-Seat": "seat-one", "X-AX-Agent": "hq", "X-AX-Model": "all-powerful"})
    assert status == 200
    assert data == {"principal": "seat-one", "tier": "read"}
    assert calls[0][2]["agent"] == "hq"


@pytest.mark.parametrize("field,value", [("from", "hq"), ("principal", "user"), ("issued_by", "user"), ("confirm_irreversible", True), ("agent", "other"), ("model", "other")])
def test_t187_body_forgery_never_reaches_destination(seat_env, row_factory, field, value, seat_authority_validation):
    service, store, _, events, _ = seat_env
    store.put(row_factory())
    app, calls = recorder(service)
    status, data = request(app, ident=service.ssh_transport_ident(1234), via="ssh", headers={"X-Ax-Seat": "seat-one"},
                           body={"destination": "approval.grant", "idempotency_key": "x", field: value})
    assert status == 403 and data["error"]["code"] == "forged_authority"
    assert calls == []
    assert any(kind == "instruction.rejected" and data["reason"] == "forged_authority" for kind, _, data in events)


def test_t187_local_header_cannot_select_seat(seat_env, row_factory, seat_authority_validation):
    service, store, *_ = seat_env
    store.put(row_factory(tier="irreversible"))
    app, _ = recorder(service)
    status, data = request(app, ident=LOCAL, method="GET", path="/v1/read/status", headers={"X-Ax-Seat": "seat-one"})
    assert status == 200 and data == {"principal": "local-operator", "tier": "operate"}


def test_t187_unknown_seat_is_auth_failure(seat_env, seat_authority_validation):
    service, _, _, events, _ = seat_env
    app, calls = recorder(service)
    status, data = request(app, ident=service.ssh_transport_ident(1234), via="ssh", method="GET", path="/v1/read/status", headers={"X-Ax-Seat": "missing"})
    assert status == 401 and calls == []
    assert events[-1][0] == "auth.failed" and events[-1][2]["reason"] == "seat_unknown"


@pytest.mark.parametrize("uid", [999, 1000, -1])
def test_t187_wrong_uid_cannot_select_seat(seat_env, monkeypatch, uid):
    service, *_ = seat_env
    monkeypatch.setenv("AX_UID", "1000")
    monkeypatch.setattr(peercred, "peer_uid", lambda conn: uid)
    assert service.authenticate_connection("ssh", object()) is None
    if uid == 1000:
        assert service.authenticate_connection("local", object()).principal == "local-operator"
    monkeypatch.setattr(peercred, "peer_uid", lambda conn: 1234)
    assert service.authenticate_connection("local", object()) is None


@pytest.mark.parametrize("uid", [1000, 1234, 999])
@pytest.mark.parametrize("destination", ["approval.grant", "approval.deny", "seat.add", "emergency"])
def test_t210_wrong_hq_uid_closes_before_dispatch(seat_env, monkeypatch, uid, destination):
    service, _, _, events, _ = seat_env
    monkeypatch.setenv("AX_HQ_UID", "5678")
    monkeypatch.setattr(peercred, "peer_uid", lambda conn: uid)
    dispatcher = SimpleNamespace(dispatch=lambda scope: pytest.fail("must not dispatch"))
    listener = SeatListener(name="hq", dispatcher=dispatcher, unix_path="unused", via_name="hq", service=service)
    conn = FakeConnection(uid, request=b"POST /v1/instructions HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}")
    listener.handle_connection(conn)
    assert conn.closed and b"403" in conn.output.getvalue()
    assert events[-1][0] == "auth.failed" and events[-1][2]["reason"] == "hq_peer_uid_mismatch"


def test_t210_peer_uid_is_rechecked_and_exceptions_fail_closed(seat_env, monkeypatch, fake_clock):
    service, _, _, events, _ = seat_env
    monkeypatch.setenv("AX_HQ_UID", "5678")
    values = iter([5678, 1000, 1000])
    monkeypatch.setattr(peercred, "peer_uid", lambda conn: next(values))
    assert service.authenticate_connection("hq", object()) == Ident("hq", "hq", "user", "irreversible", "uid=5678")
    assert service.authenticate_connection("hq", object()) is None
    assert service.authenticate_connection("hq", object()) is None
    assert sum(e[0] == "auth.failed" for e in events) == 1
    fake_clock.advance(service.defaults.get("audit_window_s"))
    monkeypatch.setattr(peercred, "peer_uid", lambda conn: (_ for _ in ()).throw(OSError()))
    assert service.authenticate_connection("hq", object()) is None
    assert sum(e[0] == "auth.failed" for e in events) == 2


@pytest.mark.parametrize("setting", ["", "invalid", "-1"])
def test_t210_missing_hq_uid_disables_only_hq_factory(seat_env, monkeypatch, setting):
    service, *_ = seat_env
    monkeypatch.setenv("AX_HQ_UID", setting)
    state = SimpleNamespace(extra_listener_factories=[], audit=None, dest_handlers={})
    app = SimpleNamespace(state=SimpleNamespace(intake=state), add_middleware=lambda *a, **k: None)
    register(app, service, gate_host="fake-intake")
    made = [factory(object()).name for factory in state.extra_listener_factories]
    assert made == ["seat", "gate-intake"]
    assert service.authenticate_connection("hq", FakeConnection(5678)) is None


def test_t210_hq_uid_alone_does_not_approve_without_user_proof(seat_env, seat_authority_validation):
    service, *_ = seat_env
    app, calls = recorder(service)
    status, _ = request(app, ident=HQ, via="hq", body={"from": "hq", "destination": "approval.grant"})
    assert status == 403 and not calls
    status, _ = request(app, ident=HQ, via="hq", body={"from": "hq", "destination": "approval.grant", "proof": "fake-user-proof"})
    assert status == 200 and len(calls) == 1


@pytest.mark.parametrize("via,ident", [("local", LOCAL), ("ssh", Ident("ssh", "commander", "", "read"))])
def test_t210_other_receptacles_cannot_impersonate_hq(seat_env, row_factory, via, ident, seat_authority_validation):
    service, store, *_ = seat_env
    store.put(row_factory())
    app, calls = recorder(service)
    status, _ = request(app, ident=ident, via=via, headers={"X-Ax-Seat": "seat-one"},
                         body={"from": "hq", "destination": "approval.grant", "idempotency_key": "one"})
    assert status == 403 and calls == []


def test_peer_uid_reads_socket_and_never_uses_claims():
    assert peercred.peer_uid(FakeConnection(44)) == 44
    conn = FakeConnection(44)
    conn.family = socket.AF_INET
    with pytest.raises(ValueError):
        peercred.peer_uid(conn)


@pytest.mark.parametrize("peer,token", [("other", "fake-core-token"), ("fake-gate", "bad"), ("", "")])
def test_t187_gate_requires_fixed_address_and_own_credential(seat_env, peer, token):
    service, *_ = seat_env
    with pytest.raises(SeatError) as error:
        service.relay(peer, RelayEvidence(token, "stable-one", "device-one"), enroll=True)
    assert error.value.reason == "unauthorized"


def test_t191_close_sessions_is_limited_to_revoked_principal(seat_env, row_factory):
    service, store, *_ = seat_env
    store.put(row_factory())
    store.put(row_factory("seat-two"))
    listener = SeatListener(name="seat", dispatcher=object(), unix_path="unused", via_name="ssh", service=service)
    first = Connection(listener, FakeConnection())
    second = Connection(listener, FakeConnection())
    first.bind(Ident("ssh", "commander", "seat-one"))
    second.bind(Ident("ssh", "commander", "seat-two"))
    listener.connections.update([first, second])
    service.effects["close_sessions"] = listener.close_principal
    service.revoke("seat-one", HQ)
    assert first.conn.closed and not second.conn.closed


def test_t191_session_limit_is_per_ssh_seat(seat_env):
    service, *_ = seat_env
    ident = Ident("ssh", "commander", "seat-one")
    limit = int(service.defaults.get("seat_max_sessions"))
    for _ in range(limit):
        service.open_session(ident)
    with pytest.raises(SeatError, match="seat_sessions_exceeded"):
        service.open_session(ident)
    service.close_session(ident)
    service.open_session(ident)
    assert service._sessions["seat-one"] == limit

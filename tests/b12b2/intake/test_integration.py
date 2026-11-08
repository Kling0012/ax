from __future__ import annotations

import json

import pytest

from core.intake.approval import ApprovalError, VerifiedUser
from core.intake.approval.integration import register
from core.intake.vias import hq_ident
from tests.fakes.b12b2.intake.parts import HQ, destination, instruction, seat


def build(world, entries=None, verifier=True):
    import yaml
    from axcommon import schemas
    from core.intake.app import build_app
    from starlette.testclient import TestClient
    config = world["tmp"] / "config"
    config.mkdir(exist_ok=True)
    entries = entries or [destination()]
    (config / "intake-destinations.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
    app = build_app(config_dir=config, ext_dir=world["tmp"] / "no-ext", dest_dir=world["tmp"] / "no-dest",
                    transport=world["transport"], sink=world["sink"], run_heartbeat_loop=False)
    world["service"].policy.schema_loader = schemas.load
    register(app, world["service"], hq_verifier=world["hq"].verify if verifier else None)
    def client(ident):
        async def authenticated(scope, receive, send):
            if scope["type"] == "http" and ident is not None:
                scope["ax_ident"] = ident
            await app(scope, receive, send)
        return TestClient(authenticated)
    return app, client


def core_instruction():
    payload = {"name": "example-change", "project": "demo", "title": "Example", "body": "Example change",
               "acceptance": {"examples": ["works"], "boundaries": ["rejects invalid"]}}
    return instruction(payload=payload)


def test_T189_b5_real_app_and_fake_hq_execute_stored_content(world):
    app, client = build(world)
    response = client(seat()).post("/v1/instructions", json=core_instruction())
    assert response.status_code == 202
    aid = response.json()["approval_id"]
    assert response.json()["status"] == "pending_user"
    assert not world["executor"].calls
    hq = client(HQ)
    detail = hq.get(f"/v1/approvals/{aid}").json()["data"]
    assert detail["payload"] == core_instruction()["payload"]
    assert hq.get("/v1/approvals").status_code == 200
    granted = hq.post(f"/v1/approvals/{aid}/grant", json={"digest": detail["digest"], "payload": {"reason": "差し替え"}})
    assert granted.status_code == 200
    assert world["executor"].calls[0]["payload"] == core_instruction()["payload"]
    assert hq.post(f"/v1/approvals/{aid}/grant", json={"digest": detail["digest"]}).status_code == 409


@pytest.mark.parametrize("uid", [1000, 2002])
def test_T190_wrong_hq_peer_uid_never_grants(world, uid):
    _, client = build(world)
    pending = client(seat()).post("/v1/instructions", json=core_instruction()).json()
    aid = pending["approval_id"]
    assert hq_ident(uid, allowed_uid=2001) is None
    denied = client(hq_ident(uid, allowed_uid=2001)).post(f"/v1/approvals/{aid}/grant",
                                                        json={"digest": world["store"].get(aid)["digest"]})
    assert denied.status_code == 403
    assert world["store"].get(aid)["state"] == "pending_user" and not world["executor"].calls


def test_T190_hq_factor_public_route_and_missing_binding_fail_closed(world):
    _, client = build(world)
    aid = client(seat()).post("/v1/instructions", json=core_instruction()).json()["approval_id"]
    data = {"digest": world["store"].get(aid)["digest"]}
    world["hq"].second = False
    assert client(HQ).post(f"/v1/approvals/{aid}/grant", json=data).status_code == 403
    world["hq"].second = True
    world["hq"].direct = False
    assert client(HQ).post(f"/v1/approvals/{aid}/grant", json=data).status_code == 404
    world["hq"].direct = True
    assert client(seat()).post(f"/v1/approvals/{aid}/grant", json=data).status_code == 403
    assert not world["executor"].calls


def test_T190_forged_confirm_and_expired_request_never_create_waiting(world):
    _, client = build(world)
    c = client(seat())
    for over, code in [({"confirm_irreversible": True}, "forged_authority"),
                       ({"expires_at": "2000-01-01T00:00:00.000Z"}, "expired")]:
        reply = c.post("/v1/instructions", json={**core_instruction(), **over})
        assert reply.status_code in (403, 422) and reply.json()["error"]["code"] == code
    assert not world["store"].list()


def test_T190_standing_permission_dispatch_and_permission_change_wait(world):
    _, client = build(world, [destination(), destination("permission.change")])
    world["service"].policy.permission_check = lambda body, ident: True
    assert client(seat()).post("/v1/instructions", json=core_instruction()).status_code == 200
    assert len(world["executor"].calls) == 1 and not world["store"].list()
    response = client(seat()).post("/v1/instructions", json=instruction("permission.change", {"project": "demo", "repo": "demo", "destination": "demo",
                                                                  "operations": ["merge"], "source_instruction": "example"}))
    assert response.status_code == 202
    assert len(world["executor"].calls) == 1


def test_T212_real_ledger_fixed_reasons_and_relay(world):
    from core.ledger import LedgerService, Reject, user_only_for
    from core.ledger.store import Store
    store = Store(world["tmp"] / "ledger.sqlite")
    ledger = LedgerService(store, None, secret_scan=lambda _: [])
    try:
        for reason in ("irreversible", "publish", "money", "secret_input", "physical", "preference", "range_review"):
            body = {"title": reason, "state": "waiting_user", "owner": "user",
                    "card": {"question": "確認するか", "waiting_reason": reason}}
            todo = ledger.create_todo(body, actor="commander")
            assert todo["user_only"] == user_only_for(reason)
            world["service"].policy.todo_reader = ledger.get_todo
            answer = instruction("ledger.answer", {"todo_id": todo["id"], "answer": "値", "quote": "発言の原文"})
            if user_only_for(reason):
                with pytest.raises(Reject, match="user_only_fixed"):
                    ledger.create_todo({**body, "user_only": False}, actor="commander")
                with pytest.raises(ApprovalError, match="user_only_card"):
                    world["service"].policy.check(answer, destination("ledger.answer", "operate"), seat())
                with pytest.raises(Reject, match="user_only_card"):
                    ledger.answer(todo["id"], {"answer": "値", "answer_source": "relay", "quote": "発言の原文", "version": 1},
                                  actor="seat-a", via="ssh")
            else:
                world["service"].policy.check(answer, destination("ledger.answer", "operate"), seat())
                result = ledger.answer(todo["id"], {"answer": "値", "answer_source": "relay", "quote": "発言の原文", "version": 1},
                                       actor="seat-a", via="ssh")
                assert result["card"]["answer"]["answer_source"] == "relay"
                assert result["card"]["answer"]["answered_by"] == "seat-a"
                assert result["state"] == "todo"
                assert not world["service"].policy.permitted(instruction("permission.change", {"evidence": todo["id"]}), seat())
    finally:
        store.close()


def test_T190_factor_verification_stays_in_fake_hq(world):
    _, client = build(world)
    aid = client(seat()).post("/v1/instructions", json=core_instruction()).json()["approval_id"]
    digest = {"digest": world["store"].get(aid)["digest"]}
    hq = world["hq"]
    assert not hq.factor_check("incorrect", now=1, expires_at=20)
    assert client(HQ).post(f"/v1/approvals/{aid}/grant", json=digest).status_code == 403
    assert not hq.factor_check("fake-factor", now=20, expires_at=20)
    assert not hq.factor_check("incorrect", now=1, expires_at=20)
    assert hq.locked
    assert not hq.factor_check("fake-factor", now=1, expires_at=20)
    assert not world["executor"].calls
    hq.locked, hq.failures = False, 0
    assert hq.factor_check("fake-factor", now=1, expires_at=20)
    assert not hq.factor_check("fake-factor", now=1, expires_at=20)
    assert client(HQ).post(f"/v1/approvals/{aid}/grant", json=digest).status_code == 403
    assert "fake-factor" not in json.dumps([c.json for c in world["transport"].calls])


def test_missing_hq_wire_binding_refuses_only_approval_routes(world):
    _, client = build(world, verifier=False)
    aid = client(seat()).post("/v1/instructions", json=core_instruction()).json()["approval_id"]
    assert client(HQ).get(f"/v1/approvals/{aid}").status_code == 200
    assert client(HQ).post(f"/v1/approvals/{aid}/grant", json={"digest": world["store"].get(aid)["digest"]}).status_code == 501
    assert client(seat()).get("/v1/destinations").status_code == 200
    assert not world["executor"].calls


def test_T194_real_destination_schemas_go_round_trip(world):
    from axcommon import schemas
    from tests.fakes.b12b2.intake.parts import ratify, removal
    world["service"].policy.schema_loader = schemas.load
    world["service"].go.schema_loader = schemas.load
    _, client = build(world, [destination("go.confirm"), destination("approval.withdraw", "operate")])
    commander, hq = client(seat()), client(HQ)
    approved = []
    for key, payload in [("ratify", ratify()), ("removal", removal())]:
        reply = commander.post("/v1/instructions", json=instruction("go.confirm", payload, key=key))
        assert reply.status_code == 202, reply.text
        aid = reply.json()["approval_id"]
        data = hq.get(f"/v1/approvals/{aid}").json()["data"]
        assert hq.post(f"/v1/approvals/{aid}/grant", json={"digest": data["digest"]}).status_code == 200
        approved.append(aid)
    assert not world["executor"].calls
    go = world["service"].go
    raw = go.export_view(approved[-1])
    assert go.verify_view(approved[-1], raw)["checks"]["go_file"] == "passed"
    withdrawn = commander.post("/v1/instructions", json=instruction("approval.withdraw", {"approval_id": approved[-1]}, key="withdraw"))
    assert withdrawn.status_code == 200
    assert go.verify_view(approved[-1], raw)["checks"]["record"] == "failed"


def test_unresolved_destination_schema_is_explicitly_unimplemented(world):
    from axcommon import schemas

    dest = destination()
    dest["handler"] = "not-implemented:core.change"
    world["service"].policy.schema_loader = schemas.load
    with pytest.raises(NotImplementedError, match="wire contract unresolved"):
        world["service"].policy.check(instruction(), dest, seat())


@pytest.mark.xfail(strict=True, reason="S-9.2/T-212f: existing b3 LedgerService.answer rejects HQ correction of a relay answer")
def test_T212_hq_can_correct_a_relay_answer(world):
    from core.ledger import LedgerService
    from core.ledger.store import Store
    store = Store(world["tmp"] / "relay-correction.sqlite")
    ledger = LedgerService(store, None, secret_scan=lambda _: [])
    try:
        todo = ledger.create_todo({"title": "確認", "state": "waiting_user", "owner": "user",
                                   "card": {"question": "確認するか", "waiting_reason": "physical"}}, actor="commander")
        ledger.answer(todo["id"], {"answer": "中継", "answer_source": "relay", "quote": "原文", "version": 1},
                      actor="seat-a", via="ssh")
        corrected = ledger.answer(todo["id"], {"answer": "訂正", "answer_source": "hq", "version": 1}, actor="user", via="hq")
        assert corrected["card"]["answer"]["answer"] == "訂正"
    finally:
        store.close()


def test_core_change_approval_submits_saved_request_to_ledger(world):
    from axcommon import schemas
    from tests.fakes.b12.work_core.peers import setup

    world["service"].executor = None
    world["service"].policy.schema_loader = schemas.load
    app, client = build(world)
    engine_app, peers, _, deployments = setup()
    app.state.b12 = engine_app.state.b12
    app.state.b12.app = app
    payload = {"name": "mapper-contract", "project": "ax", "title": "Map core change",
               "body": "Map the request to a work item.",
               "acceptance": {"examples": ["maps fields"], "boundaries": ["does not deploy"]}}
    commander, hq = client(seat()), client(HQ)
    response = commander.post("/v1/instructions", json=instruction("core.change", payload))
    assert response.status_code == 202
    approval_id = response.json()["approval_id"]
    assert not peers.todos and not peers.items
    row = world["store"].get(approval_id)
    assert hq.post(f"/v1/approvals/{approval_id}/grant", json={"digest": "wrong"}).status_code == 409
    assert not peers.todos
    granted = hq.post(f"/v1/approvals/{approval_id}/grant", json={"digest": row["digest"]})
    assert granted.status_code == 200
    assert len(peers.todos) == len(peers.items) == 1
    assert next(iter(peers.items.values()))["body"] == payload["body"]
    assert not deployments
    assert hq.post(f"/v1/approvals/{approval_id}/grant", json={"digest": row["digest"]}).status_code == 409


def test_hq_seat_enrollment_approval_and_one_time_code(world, seat_env):
    from axcommon import schemas
    from core.intake.dest.b12b3 import seat as seat_destination
    from core.hq.seats.render import completed_page
    from core.hq.seats.service import SeatService as HQSeatService

    world["service"].executor = None
    world["service"].policy.schema_loader = schemas.load
    app, client = build(world, [destination("seat.enroll_start"), destination("approval.deny", "operate")])
    app.state.seats = seat_env[0]
    seat_destination.register(app)
    hq = client(HQ)
    payload = {"label": "remote", "tier": "read"}
    request = instruction("seat.enroll_start", payload)
    created = hq.post("/v1/instructions", json=request)
    assert created.status_code == 202
    aid = created.json()["approval_id"]
    assert seat_env[1].db.execute("SELECT count(*) FROM enroll_codes").fetchone()[0] == 0
    digest = world["store"].get(aid)["digest"]
    assert hq.post(f"/v1/approvals/{aid}/grant", json={"digest": "wrong"}).status_code == 409
    assert seat_env[1].db.execute("SELECT count(*) FROM enroll_codes").fetchone()[0] == 0
    world["hq"].second = False
    assert hq.post(f"/v1/approvals/{aid}/grant", json={"digest": digest}).status_code == 403
    world["hq"].second = True
    granted = hq.post(f"/v1/approvals/{aid}/grant", json={"digest": digest})
    assert granted.status_code == 200
    code = granted.json()["enrollment_code"]
    assert code in completed_page("seat.enroll_start", HQSeatService._registration_display("seat.enroll_start", granted.json()))
    assert code not in json.dumps(hq.get(f"/v1/approvals/{aid}").json())
    assert hq.post(f"/v1/approvals/{aid}/grant", json={"digest": digest}).status_code == 409
    assert seat_env[1].db.execute("SELECT count(*) FROM enroll_codes").fetchone()[0] == 1
    pending = hq.post("/v1/instructions", json=instruction("seat.enroll_start", payload, key="deny-request"))
    denied_id = pending.json()["approval_id"]
    denied_digest = world["store"].get(denied_id)["digest"]
    denied = hq.post("/v1/instructions", json=instruction("approval.deny", {"approval_id": denied_id, "digest": denied_digest}, key="deny-action"))
    assert denied.status_code == 200
    assert hq.post(f"/v1/approvals/{denied_id}/grant", json={"digest": denied_digest}).status_code == 409
    assert seat_env[1].db.execute("SELECT count(*) FROM enroll_codes").fetchone()[0] == 1


from tests.b12b1.seat.conftest import seat_env


def test_secret_push_approval_calls_authenticated_resources(world, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from axcommon import creds, schemas
    from core.intake.dest.b12b3 import secret
    from core.resources.api import build_routes
    from core.resources.core_auth import CoreAuth
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    token = "test-intake-credential"
    token_file = world["tmp"] / "intake-token"
    token_file.write_text(token)
    monkeypatch.setenv("AX_CORE_TOKEN_FILE", str(token_file))
    sent = []
    auth = CoreAuth(creds.TokenTable({"intake": hashlib.sha256(token.encode()).hexdigest()}))
    svc = SimpleNamespace(auth=auth, secret_push=lambda slot: sent.append(slot))
    resources = TestClient(Starlette(routes=build_routes(svc, work_side=False)))
    assert resources.post("/v1/secret-push", json={"slot": "test-slot"}).status_code == 401
    assert not sent

    def transport(method, url, headers, raw, timeout):
        response = resources.request(method, "/v1/secret-push", headers=headers, content=raw)
        return response.status_code, dict(response.headers), response.content

    world["service"].executor = None
    world["service"].policy.schema_loader = schemas.load
    app, client = build(world, [destination("secret.push")])
    app.state.intake.transport = transport
    app.state.intake.dest_handlers["secret.push"] = secret.handle
    hq = client(HQ)
    pending = hq.post("/v1/instructions", json=instruction("secret.push", {"slot": "test-slot"}))
    assert pending.status_code == 202
    aid = pending.json()["approval_id"]
    assert not sent
    digest = world["store"].get(aid)["digest"]
    assert hq.post(f"/v1/approvals/{aid}/grant", json={"digest": digest}).status_code == 200
    assert sent == ["test-slot"]
    assert hq.post(f"/v1/approvals/{aid}/grant", json={"digest": digest}).status_code == 409
    assert sent == ["test-slot"]


def test_resources_secret_push_reads_registered_slot_and_calls_adapter(world, monkeypatch):
    import hashlib
    import yaml
    from axcommon import creds
    from core.resources.app import Config, build as build_resources
    from core.resources.core_auth import CoreAuth
    from core.resources.envs.adapters.ssh_host import SSHHostAdapter
    from starlette.testclient import TestClient

    config = world["tmp"] / "resources-config"
    config.mkdir()
    slots = world["tmp"] / "slots"
    slots.mkdir()
    (slots / "test-slot").write_bytes(b"example-value")
    (config / "secret-slots.yaml").write_text(yaml.safe_dump({"slots": {"test-slot": {
        "operations": ["write"], "target": {"env": "test-env", "name": "test-target"}}}}))
    monkeypatch.setenv("AX_CONFIG_DIR", str(config))
    monkeypatch.setenv("AX_HQ_SLOTS_DIR", str(slots))
    environment = {"adapter": "ssh-host", "state": "available", "secrets_allowed": False,
                   "secret_targets": [{"name": "test-target", "slot": "test-slot", "max_bytes": 64}]}
    monkeypatch.setattr("core.resources.envs.registry.load_environment_file", lambda: {"test-env": environment})
    calls = []
    class Adapter:
        def secret_put(self, target, value):
            calls.append((target, value))
            return {"ok": True}
    monkeypatch.setattr(SSHHostAdapter, "from_environment", lambda *args, **kwargs: Adapter())
    token = "fake-intake-token"
    auth = CoreAuth(creds.TokenTable({"intake": hashlib.sha256(token.encode()).hexdigest()}))
    core, _, _ = build_resources(Config(db=str(world["tmp"] / "resources.sqlite"), ext_dir=str(world["tmp"] / "no-ext")),
                                  auth=auth, run_heartbeat_loop=False)
    client = TestClient(core)
    assert client.post("/v1/secret-push", json={"slot": "test-slot"}).status_code == 401
    assert not calls
    reply = client.post("/v1/secret-push", headers={"authorization": "Bearer " + token}, json={"slot": "test-slot"})
    assert reply.status_code == 200 and reply.json() == {"ok": True}
    assert calls == [("test-target", b"example-value")]
    assert "example-value" not in reply.text


def test_snapshot_get_preserves_approvals_and_audit(world):
    _, clients = build(world)
    response = clients(seat()).post('/v1/instructions', json=core_instruction())
    aid = response.json()['approval_id']
    before = world['store'].list()
    access = world['tmp'] / 'access.log'
    access_before = access.read_bytes() if access.exists() else None
    event_count = len(world['transport'].calls)
    world['clock'].advance(7200)
    snapshot = clients(HQ).get('/v1/approval-snapshot')
    assert snapshot.status_code == 200
    assert snapshot.json()['approvals'][0]['approval_id'] == aid
    assert snapshot.json()['approvals'][0]['state'] == 'expired'
    assert world['store'].list() == before
    assert len(world['transport'].calls) == event_count
    assert (access.read_bytes() if access.exists() else None) == access_before
    assert clients(None).get('/v1/approval-snapshot').status_code == 403
    # Exercise the same saved data through HQ's authenticated public HTTP route.
    from core.hq.approval.intake import UnixIntake
    from core.hq.ext.b12b2 import register as register_hq
    from tests.b10b.test_app import app as make_hq, headers
    from tests.fakes.b10b.runtime import call
    hq_dir = world['tmp'] / 'dashboard-hq'
    hq_dir.mkdir()
    hq_app = make_hq(hq_dir)
    def transport(method, path, body):
        result = clients(HQ).request(method, path, content=body)
        return result.status_code, result.content
    hq_app.state.hq_approval_intake = UnixIntake(transport=transport)
    register_hq(hq_app)
    assert call(hq_app, 'GET', '/v1/hq/approval-snapshot').status_code == 403
    via_hq = call(hq_app, 'GET', '/v1/hq/approval-snapshot', headers=headers())
    assert via_hq.status_code == 200 and via_hq.json() == snapshot.json()
    assert world['store'].list() == before
    assert len(world['transport'].calls) == event_count
    assert (access.read_bytes() if access.exists() else None) == access_before

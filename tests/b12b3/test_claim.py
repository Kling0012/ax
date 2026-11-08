"""claim.op destination (S-6.17, S-24.1): acquire, renew, conflict, release through the real intake app.

Every call goes through `core.intake.app.build_app` with a fake receptacle; no service is contacted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from axcommon import ids
from axcommon.events import build_event
from core.intake.app import build_app
from core.intake.dest.b12b3 import claim as claim_mod
from core.intake.idempotency import IdempotencyStore
from core.intake.registry import Registry, unresolved_contract
from core.intake.vias import Ident

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "host/lib"))
import ax_cli  # noqa: E402

ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
SEAT_A = Ident("local", "commander", "seat-a", "operate")
SEAT_B = Ident("ssh", "commander", "seat-b", "operate")
HQ = Ident("hq", "hq", "user", "irreversible")
KEY = "cutover"


class RecordingSink:
    """Builds schema-validated events exactly as EventSink.emit does, and keeps them."""

    def __init__(self):
        self.events = []

    def emit(self, kind, subject, data=None, **kw):
        ev = build_event(component="intake", host="test", kind=kind, subject=subject, data=data, **kw)
        self.events.append(ev)
        return ev

    def of(self, kind):
        return [e for e in self.events if e["kind"] == kind]


@pytest.fixture
def entry(tmp_path, repo_root, monkeypatch):
    monkeypatch.setenv("AX_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "config"
    (config / "intake-destinations.d").mkdir(parents=True)
    for name in ("b12b2.yaml", "b12b3.yaml"):
        (config / "intake-destinations.d" / name).write_text(
            (repo_root / "config/intake-destinations.d" / name).read_text())
    sink = RecordingSink()
    app = build_app(config_dir=config, dest_dir=tmp_path / "no-dest", ext_dir=tmp_path / "no-ext",
                    sink=sink, idem=IdempotencyStore(None, retain_h=24), run_heartbeat_loop=False)
    clock = [datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)]
    handler = claim_mod.register(app, path=tmp_path / "state/intake/claims.json", now=lambda: clock[0],
                                 lease=timedelta(minutes=30), max_hold=timedelta(hours=4))
    ident = [SEAT_A]

    async def mouth(scope, receive, send):
        scope["ax_ident"] = ident[0]
        await app(scope, receive, send)

    client = TestClient(mouth)
    world = SimpleNamespace(app=app, client=client, ident=ident, sink=sink, clock=clock, handler=handler,
                            path=tmp_path / "state/intake/claims.json")
    yield world
    client.close()
    app.state.intake.executor.shutdown()


def post(world, who, op, target_key=KEY, *, session="sess-1", key=None):
    world.ident[0] = who
    body = {"id": ids.new_ulid(), "from": who.from_, "destination": "claim.op",
            "payload": {"op": op, "target_key": target_key}, "idempotency_key": key or ids.new_ulid(),
            "session": session}
    response = world.client.post("/v1/instructions", json=body)
    return response.status_code, response.json()


def test_repo_registry_resolves_claim_op_to_b12b3_handler(repo_root):
    registry = Registry.load(repo_root / "config")
    row = registry.get("claim.op")
    assert row["handler"] == "b12b3:claim.op"
    assert unresolved_contract(row) is None
    assert (row["tier"], row["ack"], row["mcp_tool"]) == ("operate", "sync", "ax_claim")
    others = {n for n in registry.names() if unresolved_contract(registry.get(n))}
    assert "claim.op" not in others and others  # the remaining unresolved rows are untouched


def test_package_register_uses_intake_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AX_STATE_DIR", str(tmp_path / "state"))
    from core.intake.dest import b12b3

    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(dest_handlers={}, sink=None), ext_failures=[]))
    b12b3.register(app)
    handler = app.state.intake.dest_handlers["claim.op"]
    assert handler.file.path == tmp_path / "state/intake/claims.json"
    assert app.state.intake.claim_store is handler.store and app.state.ext_failures == []


def test_acquire_returns_verified_owner_and_granted_event_id(entry):
    status, out = post(entry, SEAT_A, "acquire")
    assert status == 200, out
    data = out["result"]
    assert data["op"] == "acquire" and data["holder"] is True and data["target_key"] == KEY
    assert data["owner"] == {"principal": "seat-a", "session": "sess-1", "via": "local"}
    assert data["lease_until"] == "2026-11-08T00:30:00Z"
    assert ULID.fullmatch(data["claim_event_id"])
    granted = entry.sink.of("claim.granted")
    assert [e["id"] for e in granted] == [data["claim_event_id"]]
    assert granted[0]["data"]["lease_expires_at"] == data["lease_until"]
    assert granted[0]["source"]["principal"] == "seat-a"
    assert json.loads(entry.path.read_text())[KEY]["owner"]["principal"] == "seat-a"


def test_owner_renew_extends_lease_and_returns_renewed_event(entry):
    post(entry, SEAT_A, "acquire")
    entry.clock[0] += timedelta(minutes=20)
    status, out = post(entry, SEAT_A, "renew")
    assert status == 200, out
    data = out["result"]
    assert data["op"] == "renew" and data["holder"] is True
    assert data["lease_until"] == "2026-11-08T00:50:00Z"
    assert data["claim_event_id"] == entry.sink.of("claim.renewed")[-1]["id"]
    assert json.loads(entry.path.read_text())[KEY]["lease_until"] == "2026-11-08T00:50:00Z"


def test_renew_stops_at_max_hold(entry):
    post(entry, SEAT_A, "acquire")
    for _ in range(9):
        entry.clock[0] += timedelta(minutes=25)
        assert post(entry, SEAT_A, "renew")[0] == 200
    entry.clock[0] = datetime(2026, 11, 8, 3, 50, tzinfo=timezone.utc)
    status, out = post(entry, SEAT_A, "renew")
    assert status == 200 and out["result"]["lease_until"] == "2026-11-08T04:00:00Z"


def test_other_commander_conflicts_without_changing_the_claim(entry):
    _, first = post(entry, SEAT_A, "acquire")
    before = entry.path.read_text()
    for op in ("acquire", "renew", "release"):
        status, out = post(entry, SEAT_B, op, session="sess-b")
        assert status == 409, (op, out)
        assert out["ok"] is False
        assert out["result"]["error"]["code"] == "conflict"
        assert out["result"]["data"]["reason"] == "not_holder"
        assert out["result"]["data"]["owner"]["principal"] == "seat-a"
        assert out["result"]["data"]["lease_until"] == first["result"]["lease_until"]
    assert entry.path.read_text() == before
    # Same seat in a different session is a different owner (S-6.17).
    assert post(entry, SEAT_A, "renew", session="sess-2")[0] == 409


def test_renew_or_release_without_claim_is_no_claim(entry):
    for op in ("renew", "release"):
        status, out = post(entry, SEAT_A, op, "nothing.here")
        assert status == 409
        assert out["result"]["error"]["code"] == "conflict"
        assert out["result"]["data"]["reason"] == "no_claim"


def test_release_by_owner_then_other_can_acquire(entry):
    post(entry, SEAT_A, "acquire")
    status, out = post(entry, SEAT_A, "release")
    assert status == 200 and out["result"]["holder"] is False
    assert out["result"]["claim_event_id"] == entry.sink.of("claim.released")[-1]["id"]
    assert json.loads(entry.path.read_text()) == {}
    status, out = post(entry, SEAT_B, "acquire", session="sess-b")
    assert status == 200 and out["result"]["owner"]["principal"] == "seat-b"


def test_expired_claim_is_replaced(entry):
    post(entry, SEAT_A, "acquire")
    entry.clock[0] += timedelta(minutes=31)
    status, out = post(entry, SEAT_B, "acquire", session="sess-b")
    assert status == 200 and out["result"]["owner"]["principal"] == "seat-b"
    assert [e["data"]["principal"] for e in entry.sink.of("claim.expired")] == ["seat-a"]


def test_hq_preempts_and_previous_owner_is_told(entry):
    post(entry, SEAT_A, "acquire")
    status, out = post(entry, HQ, "acquire", session="hq-1")
    assert status == 200 and out["result"]["owner"]["principal"] == "user"
    preempted = entry.sink.of("claim.preempted")
    assert preempted[0]["data"]["principal"] == "seat-a" and preempted[0]["data"]["by"]["principal"] == "user"
    assert post(entry, SEAT_A, "renew")[0] == 409


def test_claims_survive_a_restart(entry, tmp_path):
    post(entry, SEAT_A, "acquire")
    restarted = claim_mod.register(entry.app, path=entry.path, now=lambda: entry.clock[0],
                                   lease=timedelta(minutes=30), max_hold=timedelta(hours=4))
    assert restarted is not entry.handler
    status, out = post(entry, SEAT_B, "acquire", session="sess-b")
    assert status == 409 and out["result"]["data"]["owner"]["principal"] == "seat-a"


def test_unreadable_store_answers_unavailable_and_is_not_overwritten(entry):
    entry.path.parent.mkdir(parents=True, exist_ok=True)
    entry.path.write_text("{broken")
    entry.handler.file.reload()
    status, out = post(entry, SEAT_A, "acquire")
    assert status == 503 and out["result"]["error"]["code"] == "unavailable"
    assert entry.path.read_text() == "{broken"
    entry.path.write_text("{}")  # repaired by hand
    assert post(entry, SEAT_A, "acquire")[0] == 200


def test_unwritable_store_rolls_back(entry, monkeypatch):
    def fail():
        raise OSError("read-only")

    monkeypatch.setattr(entry.handler.file, "save", fail)
    status, out = post(entry, SEAT_A, "acquire")
    assert status == 503 and out["result"]["error"]["code"] == "unavailable"
    assert dict(entry.handler.file) == {}


def cli(entry, who, *argv):
    def transport(method, path, body):
        entry.ident[0] = who
        response = entry.client.request(method, path, json=body)
        return response.status_code, response.json()

    out = StringIO()
    code = ax_cli.main(list(argv), transport=transport, environ={"AX_SESSION": "cli-1"}, stdout=out,
                       stderr=StringIO())
    return code, json.loads(out.getvalue())


def test_cli_acquire_and_renew_cutover_give_the_s_24_1_shape(entry):
    code, out = cli(entry, SEAT_A, "claim", "acquire", KEY, "--json")
    assert code == 0 and out["ok"] is True and out["op"] == "claim"
    code, out = cli(entry, SEAT_A, "claim", "renew", KEY, "--json", "--request-id", ids.new_ulid())
    assert code == 0, out
    data = out["data"]
    assert data["holder"] is True and data["op"] == "renew"
    assert data["owner"] == {"principal": "seat-a", "session": "cli-1", "via": "local"}
    assert ULID.fullmatch(data["claim_event_id"])


def test_cli_conflict_exits_8_with_owner(entry):
    cli(entry, SEAT_A, "claim", "acquire", KEY, "--json")
    code, out = cli(entry, SEAT_B, "claim", "renew", KEY, "--json")
    assert code == 8
    assert out["ok"] is False and out["error"]["code"] == "conflict"
    assert out["data"]["reason"] == "not_holder" and out["data"]["owner"]["principal"] == "seat-a"
    code, out = cli(entry, SEAT_B, "claim", "release", "other.key", "--json")
    assert code == 8 and out["data"]["reason"] == "no_claim"


@pytest.mark.parametrize("argv", [["claim", "steal", KEY], ["claim", "acquire"], ["claim", "acquire", "bad key"]])
def test_cli_rejects_bad_arguments_without_calling_the_entry(entry, argv):
    def transport(method, path, body):
        raise AssertionError("must not call the entry")

    out = StringIO()
    code = ax_cli.main(argv + ["--json"], transport=transport, environ={}, stdout=out, stderr=StringIO())
    assert code == 1 and json.loads(out.getvalue())["error"]["code"] == "bad_request"


def test_claim_trusted_metadata_uses_ledger_and_preserves_legacy(entry):
    entry.app.state.b12_read_relay = SimpleNamespace(clients={'ledger': SimpleNamespace(
        get=lambda path: {'id': 'todo-1', 'project': 'project-p'} if path == '/v1/todos/todo-1' else {})})
    body = {'payload': {'op': 'acquire', 'target_key': 'card:todo-1'}, 'session': 'sess-1',
            'agent': 'forged-body', 'model': 'forged-body'}
    code, result = entry.handler.handle_with_context(entry.app, {}, body, SEAT_A,
                                                       {'agent': 'trusted-agent', 'model': 'trusted-model'})
    assert code == 200 and result['holder']
    stored = json.loads(entry.path.read_text())['card:todo-1']['metadata']
    assert stored == {'agent': 'trusted-agent', 'model': 'trusted-model', 'project': 'project-p'}
    post(entry, SEAT_A, 'acquire', target_key='legacy')
    entry.handler.file['legacy'].pop('metadata', None)
    code, _ = entry.handler.handle_with_context(entry.app, {},
                 {'payload': {'op': 'renew', 'target_key': 'legacy'}, 'session': 'sess-1'},
                 SEAT_A, {'agent': 'another'})
    assert code == 200 and 'metadata' not in entry.handler.file['legacy']
    code, _ = entry.handler.handle_with_context(entry.app, {},
                 {'payload': {'op': 'acquire', 'target_key': 'card:unknown'}, 'session': 'sess-1'},
                 SEAT_A, {})
    assert code == 200 and 'project' not in entry.handler.file['card:unknown']['metadata']


def test_claim_to_brief_uses_authenticated_metadata(entry):
    from core.intake.dest.b12.read_relay import register as register_reads
    from core.intake.seat.middleware import SeatMiddleware
    from core.intake.seat.service import SeatService

    register_reads(entry.app)
    relay = entry.app.state.b12_read_relay
    relay.clients['ledger'].get = lambda path, params=None: {'id': 'todo-1', 'project': 'project-p'}
    entry.handler.store._now = lambda: datetime.now(timezone.utc)
    class FakeSeat:
        store = None
        local_tier = staticmethod(lambda ident: ident)
        assert_body = SeatService.assert_body
        check_available = staticmethod(lambda ident, body: None)
        count_instruction = staticmethod(lambda ident: None)
        count_result = staticmethod(lambda ident, ok: None)
        emit = staticmethod(lambda *args: None)
    guarded = SeatMiddleware(entry.app, service=FakeSeat(), state=entry.app.state.intake)
    identity = [SEAT_A]
    async def mouth(scope, receive, send):
        scope['ax_ident'] = identity[0]
        await guarded(scope, receive, send)
    with TestClient(mouth) as client:
        def invoke(argv):
            out = StringIO()
            def transport(method, path, body, headers=None):
                response = client.request(method, path, json=body, headers=headers)
                return response.status_code, response.json()
            code = ax_cli.main(argv, transport=transport, environ={}, stdout=out, stderr=StringIO())
            return code, json.loads(out.getvalue())
        code, _ = invoke(['--agent', 'agent-a', '--model', 'model-a', '--session', 'session-a',
                          'claim', 'acquire', 'card:todo-1'])
        assert code == 0
        code, out = invoke(['brief', '--section', 'claims', '--project', 'project-p'])
        assert code == 0
        claim = out['data']['sections'][0]['items'][0]
        assert claim['owner']['principal'] == 'seat-a'
        assert claim['agent'] == 'agent-a' and claim['model'] == 'model-a' and claim['project'] == 'project-p'
        identity[0] = Ident('local', 'commander', 'seat-b', 'operate')
        code, _ = invoke(['--session', 'session-b', 'claim', 'release', 'card:todo-1'])
        assert code == 8
        assert entry.handler.file['card:todo-1']['owner']['principal'] == 'seat-a'
        forged = {'from': 'commander', 'destination': 'claim.op',
                  'payload': {'op': 'acquire', 'target_key': 'forged'},
                  'idempotency_key': ids.new_ulid(), 'agent': 'forged'}
        assert client.post('/v1/instructions', json=forged).status_code >= 400
        assert 'forged' not in entry.handler.file
        code, out = invoke(['brief', '--section', 'claims', '--project', 'other'])
        assert code == 0 and out['data']['sections'][0]['items'] == []

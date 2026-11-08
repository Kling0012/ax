"""Work requests and controls through intake handlers and service HTTP APIs."""
import importlib
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient

from axcommon.client import LedgerClient
from axcommon.creds import TokenTable, sha256_hex
from core.intake.dest.b12 import work
from core.intake.dest.b12.common import Engine
from core.intake.dest.b12.common.store import Store as IntakeStore
from core.intake.dest.b12b3 import work_ops
from core.intake.vias import Ident
from core.ledger import LedgerService
from core.ledger.app import create_core_app
from core.ledger.store import Store as LedgerStore


@pytest.fixture
def system(tmp_path, monkeypatch):
    from axcommon.creds import sha256_hex
    table = tmp_path / "pool-core-tokens"
    table.write_text("intake " + sha256_hex("test-intake") + "\n")
    monkeypatch.setenv("AX_CORE_TOKENS", str(table))
    monkeypatch.setenv("AX_SECRETS_PATH", str(tmp_path))
    (tmp_path / "core-token-intake").write_text("test-intake")
    ledger_service = LedgerService(LedgerStore(tmp_path / "ledger.sqlite"), None)
    ledger_app = create_core_app(ledger_service)
    ledger_app.state.core_tokens = TokenTable({"intake": sha256_hex("test-intake")})
    pool_module = importlib.import_module("plugins.resource.worker-pool.app")
    pool_app, _, pool_service = pool_module.build(pool_module.Config(db=str(tmp_path / "pool.sqlite")), run_heartbeat_loop=False)
    ledger, pool = TestClient(ledger_app), TestClient(pool_app, headers={"Authorization": "Bearer test-intake"})
    monkeypatch.setattr(work_ops, "_token", lambda: "test-intake")

    def transport(method, url, headers, raw, timeout):
        parsed = urlsplit(url)
        client = ledger if parsed.hostname == "ledger" else pool
        response = client.request(method, parsed.path + (("?" + parsed.query) if parsed.query else ""), headers=headers, content=raw)
        return response.status_code, {}, response.content

    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=transport, dest_handlers={}), ext_failures=[]))
    app.state.b12 = Engine(app, store=IntakeStore(tmp_path / "intake.sqlite"))
    app.state.b12.ledger = LedgerClient("http://ledger:8080", token="test-intake", transport=transport)
    def create(key, owner="alice"):
        ident = Ident("ssh", "commander", owner, "operate")
        code, result = work.handle(app, {}, {"idempotency_key": key, "payload": {"project": "ax", "title": key, "role": "research"}}, ident)
        assert code == 200
        return result
    yield SimpleNamespace(app=app, ledger=ledger, pool=pool, pool_service=pool_service, create=create)
    ledger.close()
    pool.close()
    ledger_service.store.close()
    pool_service.store.close()


def test_cancel_queued_owner_and_boundaries(system):
    first, other = system.create("first"), system.create("other", "bob")
    alice = Ident("ssh", "commander", "alice", "operate")
    bob = Ident("ssh", "commander", "bob", "operate")
    denied = Ident("ssh", "commander", "alice", "read")
    def cancel(payload, ident):
        return work_ops.cancel(system.app, {}, {"payload": payload}, ident)
    assert cancel({"job_id": first["job_id"], "reason": "stop"}, denied)[0] == 403
    assert cancel({"todo_id": first["todo_id"], "reason": "stop"}, bob)[0] == 403
    assert cancel({"job_id": "no-such-job", "reason": "stop"}, alice)[0] == 404
    assert system.ledger.get('/v1/todos/' + first['todo_id'] + '?history=1').json()['history'][-1]['kind'] != 'work.requested_stop'
    status, result = cancel({"todo_id": first["todo_id"], "reason": "stop"}, alice)
    assert status == 200 and result["state"] == "cancelled"
    assert system.pool.get('/v1/jobs/' + other['job_id']).json()['state'] == 'queued'
    history = system.ledger.get('/v1/todos/' + first['todo_id'] + '?history=1').json()['history']
    assert history[-1]['kind'] == 'work.requested_stop'
    assert history[-1]['after']['principal'] == 'alice'
    before = len(history)
    assert cancel({"job_id": first["job_id"], "reason": "again"}, alice)[1]['state'] == 'cancelled'
    assert len(system.ledger.get('/v1/todos/' + first['todo_id'] + '?history=1').json()['history']) == before


def test_active_stop_only_requests_cancellation(system):
    target, other = system.create("active"), system.create("other")
    system.pool_service.store.set_state(target['job_id'], 'running')
    status, result = work_ops.stop(system.app, {}, {"payload": {"job_id": target['job_id']}}, Ident("ssh", "commander", "alice", "operate"))
    assert status == 202 and result['status'] == 'stop_requested' and result['state'] == 'running'
    assert system.pool.get('/v1/jobs/' + target['job_id']).json()['state'] == 'running'
    assert system.pool.get('/v1/jobs/' + target['job_id']).json()['cancel_requested'] is True
    assert system.pool.get('/v1/jobs/' + other['job_id']).json()['cancel_requested'] is False
    history = system.ledger.get('/v1/todos/' + target['todo_id'] + '?history=1').json()['history']
    assert history[-1]['after']['operation'] == 'work.stop'


def test_user_only_card_remains_unchanged_for_stop(system):
    headers = {"Authorization": "Bearer test-intake"}
    todo_id = "private-card"
    todo = {"id": todo_id, "title": "approval", "owner": "user", "state": "waiting_user",
            "card": {"question": "Proceed?", "waiting_reason": "irreversible"}}
    assert system.ledger.post('/v1/todos', json=todo, headers=headers).status_code == 201
    job = system.pool.post('/v1/jobs', json={"role": "research", "prompt": "look", "sensitivity": "private"}).json()['job_id']
    system.app.state.b12.db.put('read:card', {"todo_id": todo_id, "job_id": job, "owner": "alice"})
    ident = Ident("ssh", "commander", "alice", "operate")
    assert work_ops.cancel(system.app, {}, {"payload": {"todo_id": todo_id, "reason": "cancel"}}, ident)[0] == 403
    assert system.pool.get('/v1/jobs/' + job).json()['state'] == 'queued'
    status, result = work_ops.stop(system.app, {}, {"payload": {"todo_id": todo_id}}, ident)
    assert status == 200 and result['state'] == 'cancelled'
    after = system.ledger.get('/v1/todos/' + todo_id).json()
    assert after['state'] == 'waiting_user' and after['user_only'] is True
    assert after['card']['answer'] is None


def test_terminal_todo_is_unchanged_and_unknown_todo_is_rejected(system):
    target = system.create('finished')
    headers = {"Authorization": "Bearer test-intake"}
    assert system.ledger.post('/v1/todos/' + target['todo_id'] + '/transition', json={'to': 'done'}, headers=headers).status_code == 200
    ident = Ident("ssh", "commander", "alice", "operate")
    assert work_ops.stop(system.app, {}, {"payload": {"todo_id": "unknown"}}, ident)[0] == 404
    status, result = work_ops.stop(system.app, {}, {"payload": {"todo_id": target['todo_id']}}, ident)
    assert status == 200 and result['state'] == 'done'
    assert system.pool.get('/v1/jobs/' + target['job_id']).json()['state'] == 'queued'


def test_registered_intake_work_control(system, tmp_path, repo_root, monkeypatch):
    from core.intake.app import build_app
    from core.intake.dest import b12b3
    from core.intake.idempotency import IdempotencyStore
    from axcommon import ids

    monkeypatch.setenv('AX_STATE_DIR', str(tmp_path / 'intake-state'))
    app = build_app(config_dir=repo_root / 'config', dest_dir=tmp_path / 'no-dest',
                    ext_dir=tmp_path / 'no-ext', idem=IdempotencyStore(None, retain_h=24),
                    run_heartbeat_loop=False)
    app.state.intake.transport = system.app.state.intake.transport
    app.state.b12 = system.app.state.b12
    b12b3.register(app)
    current = [None]
    async def mouth(scope, receive, send):
        if current[0] is not None:
            scope['ax_ident'] = current[0]
        await app(scope, receive, send)
    client = TestClient(mouth)
    def request(job_id):
        return client.post('/v1/instructions', json={'from': 'commander', 'destination': 'work.cancel',
            'idempotency_key': ids.new_ulid(), 'payload': {'job_id': job_id, 'reason': 'requested'}})
    queued = system.create('registered-queued')
    running = system.create('registered-running')
    system.pool_service.store.set_state(running['job_id'], 'running')
    assert request(queued['job_id']).status_code in (401, 403)
    current[0] = Ident('ssh', 'commander', 'alice', 'operate')
    response = request(queued['job_id'])
    assert response.status_code == 200 and response.json()['result']['state'] == 'cancelled'
    response = request(running['job_id'])
    assert response.status_code == 202 and response.json()['result']['status'] == 'stop_requested'
    assert system.pool.get('/v1/jobs/' + running['job_id']).json()['state'] == 'running'
    client.close()
    app.state.intake.executor.shutdown()

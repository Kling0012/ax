"""Public work requests, real worker-pool and ledger HTTP apps and SQLite."""
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from core.intake.app import build_app
from core.intake.dest.b12 import work
from core.intake.dest.b12.common import Engine
from core.intake.dest.b12.common.store import Store
from core.intake.vias import Ident
from tests.b5.helpers import instruction, simple_dest, write_registry
from tests.b12b3.work_control.test_work_control_e2e import system


class Timeline:
    def __init__(self):
        self.events = []
    def get_events(self, *, kind, after, wait, limit):
        rows = [ev for ev in self.events if ev['kind'] == kind and ev['seq'] > after][:limit]
        return {'events': rows, 'next_after': rows[-1]['seq'] if rows else after}


@pytest.fixture
def world(system, tmp_path):
    config = write_registry(tmp_path / 'request-config', [simple_dest('work.request',
        schema='schemas/dest/work.request.json', handler='b12:work.request', tier='operate')])
    app = build_app(config_dir=config, dest_dir=tmp_path / 'no-dest', ext_dir=tmp_path / 'no-ext',
                    transport=system.app.state.intake.transport, run_heartbeat_loop=False)
    engine = system.app.state.b12
    engine.timeline = Timeline()
    app.state.b12 = engine
    work.register(app)
    async def mouth(scope, receive, send):
        if scope['type'] == 'http':
            scope['ax_ident'] = Ident('local', 'commander', 'alice', 'operate')
        await app(scope, receive, send)
    client = TestClient(mouth)
    def submit(key='request'):
        payload = {'project': 'ax', 'title': key, 'role': 'research', 'body': 'read',
                   'target': 'repo', 'revision': 'a' * 40}
        result = client.post('/v1/instructions', json=instruction('work.request', payload, idempotency_key=key))
        assert result.status_code == 200, result.text
        return result.json()['result']
    yield SimpleNamespace(**{'system': system, 'app': app, 'engine': engine, 'client': client, 'submit': submit})
    client.close()
    app.state.intake.executor.shutdown()


def job_done(world, request, *, state='ok', status='ready', rc=0):
    store = world.system.pool_service.store
    store.record_result(request['job_id'], status=status, text='report', human_action='', verify_rc=rc)
    store.set_state(request['job_id'], state, reason='worker_failed' if state != 'ok' else 'verified')


def todo(world, request):
    return world.system.ledger.get('/v1/todos/' + request['todo_id'] + '?history=1').json()


def event(request, source='worker-pool', job_id=None):
    return {'kind': 'job.state', 'source': {'component': source},
            'data': {'job_id': job_id or request['job_id'], 'state': 'ok'}}


def transition(world, request, to):
    response = world.system.ledger.post('/v1/todos/' + request['todo_id'] + '/transition',
        json={'to': to}, headers={'Authorization': 'Bearer test-intake'})
    assert response.status_code == 200, response.text


def test_verified_success_and_event_validation(world):
    request = world.submit()
    assert todo(world, request)['state'] == 'running'
    job_done(world, request)
    world.engine.event(event(request, source='fake'))
    world.engine.event(event(request, job_id='unrelated'))
    assert todo(world, request)['state'] == 'running'
    world.engine.event(event(request))
    result = todo(world, request)
    assert result['state'] == 'review'
    world.engine.event(event(request))
    assert len(todo(world, request)['history']) == len(result['history'])


@pytest.mark.parametrize('state,status,rc', [('failed', 'failed', 1), ('ok', 'ready', None), ('ok', 'blocked', 0)])
def test_missing_event_or_unverified_result_is_parked_after_restart(world, tmp_path, state, status, rc):
    request = world.submit()
    world.engine.event(event(request))  # The event arrives before actual termination.
    assert todo(world, request)['state'] == 'running'
    job_done(world, request, state=state, status=status, rc=rc)
    old = world.engine
    old.store.close()
    new = Engine(world.system.app, store=Store(tmp_path / 'intake.sqlite'))
    new.ledger, new.pool, new.timeline = old.ledger, old.pool, Timeline()
    world.engine = world.system.app.state.b12 = new
    world.app.state.b12 = new
    new.poll()
    result = todo(world, request)
    assert result['state'] == 'parked' and result['reason'] and result['check_at']
    new.poll()
    assert len(todo(world, request)['history']) == len(result['history'])


def test_user_changes_and_aba_are_preserved(world):
    request = world.submit()
    transition(world, request, 'todo')
    transition(world, request, 'running')
    job_done(world, request)
    before = todo(world, request)
    world.engine.event(event(request))
    after = todo(world, request)
    assert after['state'] == 'running' and after['history'] == before['history']


def test_user_change_during_conditional_transition_is_preserved(world, monkeypatch):
    request = world.submit()
    job_done(world, request)
    ledger = world.engine.ledger
    original = ledger.transition
    def race(todo_id, to, reason=None, **extra):
        if to == 'review':
            transition(world, request, 'todo')
            transition(world, request, 'running')
        return original(todo_id, to, reason, **extra)
    monkeypatch.setattr(ledger, 'transition', race)
    world.engine.event(event(request))
    assert todo(world, request)['state'] == 'running'
    assert todo(world, request)['history'][-1]['kind'] == 'transition:todo->running'


def test_early_terminal_is_recovered_after_restart_before_running(world, tmp_path, monkeypatch):
    old = world.engine
    original_start = old.start_read
    monkeypatch.setattr(old, 'start_read', lambda key, rec: None)
    request = world.submit()
    assert todo(world, request)['state'] == 'todo'
    job_done(world, request)
    monkeypatch.setattr(old, 'start_read', original_start)
    old.store.close()
    new = Engine(world.system.app, store=Store(tmp_path / 'intake.sqlite'))
    new.ledger, new.pool, new.timeline = old.ledger, old.pool, Timeline()
    world.engine = world.system.app.state.b12 = new
    world.app.state.b12 = new
    new.poll()
    assert todo(world, request)['state'] == 'review'
    assert [x['kind'] for x in todo(world, request)['history']] == [
        'created', 'transition:todo->running', 'transition:running->review']


def test_legacy_record_without_history_version_never_changes_todo(world):
    request = world.submit()
    key = 'read:request'
    rec = world.engine.db.get(key)
    rec.pop('initial_seq')
    rec.pop('running_seq')
    world.engine.db.put(key, rec)
    job_done(world, request)
    before = todo(world, request)
    world.engine.poll()
    assert todo(world, request)['history'] == before['history']


def test_actual_ledger_compare_is_atomic_for_aba(world):
    request = world.submit()
    before = todo(world, request)
    old_seq = before['history'][-1]['seq']
    transition(world, request, 'todo')
    transition(world, request, 'running')
    response = world.system.ledger.post('/v1/todos/' + request['todo_id'] + '/transition',
        json={'to': 'review', 'expected_history_seq': old_seq},
        headers={'Authorization': 'Bearer test-intake'})
    assert response.status_code == 409
    assert todo(world, request)['state'] == 'running'


def test_timeline_poll_ignores_untrusted_job_event_and_advances_cursor(world):
    request = world.submit()
    world.engine.timeline.events = [{**event(request, source='fake'), 'seq': 1},
                                    {**event(request), 'seq': 2}]
    world.engine.poll()  # Both events arrive before the job is terminal.
    assert todo(world, request)['state'] == 'running'
    assert world.engine.db.get('cursor:job.state') == 2
    job_done(world, request)
    world.engine.poll()  # No new event; periodic reconciliation finds the terminal job.
    assert todo(world, request)['state'] == 'review'
    history = todo(world, request)['history']
    world.engine.poll()
    assert todo(world, request)['history'] == history


def test_competing_conditional_transitions_have_only_one_winner(world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    request = world.submit()
    seq = todo(world, request)['history'][-1]['seq']
    barrier = Barrier(2)
    def change(to):
        barrier.wait()
        return world.system.ledger.post('/v1/todos/' + request['todo_id'] + '/transition',
            json={'to': to, 'expected_history_seq': seq},
            headers={'Authorization': 'Bearer test-intake'}).status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        statuses = list(workers.map(change, ('review', 'done')))
    assert sorted(statuses) == [200, 409]
    assert len(todo(world, request)['history']) == 3

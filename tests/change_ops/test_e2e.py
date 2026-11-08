"""Exercise authenticated intake through the real merge flow and fake GitHub."""
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from axcommon import ids
from core.intake.app import build_app
from core.intake.dest.b12b3.change import ChangeLedger, register
from core.intake.idempotency import IdempotencyStore
from core.intake.vias import Ident
from plugins.subloop.change.integration import ChangeFlow, Journal
from tests.fakes.b14.integration import Clock, make_ports, target, item, HEAD, MERGED

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def world(tmp_path):
    config = tmp_path / 'config'
    config.mkdir()
    entries = yaml.safe_load((ROOT / 'config/intake-destinations.d/b12b3-change.yaml').read_text())
    (config / 'intake-destinations.yaml').write_text(yaml.safe_dump(entries))
    app = build_app(config_dir=config, dest_dir=tmp_path / 'empty', ext_dir=tmp_path / 'empty',
                    idem=IdempotencyStore(None, retain_h=24), run_heartbeat_loop=False)
    ledger = ChangeLedger(tmp_path / 'operations.db', ['owner/issues'], resolve_pr=lambda repo, number: {'issue_repo': repo, 'number': number, 'merged': False, 'untrusted': True, 'head_sha': HEAD})
    app.state.change_ledger = ledger
    register(app)
    who = [Ident('local', 'commander', 'owner', 'operate')]
    async def authenticated(scope, receive, send):
        scope['ax_ident'] = who[0]
        await app(scope, receive, send)
    client = TestClient(authenticated)
    clock = Clock()
    fake = make_ports(clock)
    journal = Journal(tmp_path / 'journal.db', scrub=fake.ports.scrub, clock=clock)
    journal.change_ledger = ledger
    flow = ChangeFlow(fake.ports, journal, [target()], clock=clock)
    def post(dest, payload, key=None):
        return client.post('/v1/instructions', json={'id': ids.new_ulid(), 'from': who[0].from_,
            'destination': dest, 'payload': payload, 'idempotency_key': key or ids.new_ulid()})
    def request(key='request-1'):
        return post('change.request', {'issue_repo': 'owner/issues', 'kind': 'pr', 'number': 101, 'dry_run': False}, key)
    def cancel():
        return post('change.cancel_merge', {'issue_repo': 'owner/issues', 'number': 101, 'reason': 'stop'})
    def merge():
        it = {**item(), 'number': 101, 'pr': 101, 'request_id': 'request-1'}
        live = fake.github.view(it, target())
        safe = fake.safety.review({**it, 'snapshot': live})
        verified = fake.verification.verify(it, target(), head_sha=live['head_sha'], base_sha=live['base_sha'])
        return flow.merge(it, target(), safe, verified)
    yield app, ledger, who, fake, request, cancel, merge, post
    client.close()
    app.state.intake.executor.shutdown()
    journal.close()


def test_cancel_before_merge_and_replay(world):
    _, ledger, _, fake, request, cancel, merge, _ = world
    assert request().status_code == 200
    assert request().status_code == 200
    assert cancel().status_code == 200
    assert merge()['reason'] == 'merge_not_waiting'
    assert not fake.github.merges
    assert ledger.get('request-1')['state'] == 'cancelled'


def test_cancel_during_merge_is_rejected(world):
    _, ledger, _, fake, request, cancel, merge, _ = world
    assert request().status_code == 200
    seen = []
    def during(gh, repo, pr, sha, method):
        seen.append(cancel().status_code)
        gh.merges.append((repo, pr, sha, method))
        return {'merged': True, 'head_sha': sha, 'merge_commit_sha': MERGED}
    fake.github.on_merge = during
    assert merge()['reason'] == 'release_pending'
    assert seen == [409] and len(fake.github.merges) == 1
    assert ledger.get('request-1')['state'] == 'merged'
    assert cancel().status_code == 409
    assert len(fake.github.merges) == 1


def test_uncertain_merge_cannot_be_cancelled(world):
    _, ledger, _, fake, request, cancel, merge, _ = world
    assert request().status_code == 200
    def uncertain(*args):
        raise OSError('lost result')
    fake.github.on_merge = uncertain
    assert merge()['reason'] == 'merge_unconfirmed'
    assert cancel().status_code == 409
    assert ledger.get('request-1')['state'] == 'unknown'


def test_unauthorized_other_owner_and_unknown_target(world):
    _, ledger, who, _, request, cancel, _, post = world
    assert request().status_code == 200
    who[0] = Ident('local', 'commander', 'other', 'operate')
    assert cancel().status_code == 403
    who[0] = Ident('local', 'commander', 'owner', 'read')
    assert cancel().status_code == 403
    who[0] = Ident('local', 'commander', 'owner', 'operate')
    result = post('change.cancel_merge', {'issue_repo': 'owner/issues', 'number': 999, 'reason': 'stop'})
    assert result.status_code == 404
    assert ledger.get('request-1')['state'] == 'waiting'


def test_failure_returns_to_waiting_and_records_reason(world):
    _, ledger, _, fake, request, cancel, merge, _ = world
    assert request().status_code == 200
    fake.github.on_merge = lambda *args: {'merged': False, 'definitive': True}
    assert merge()['reason'] == 'merge_failed'
    assert ledger.get('request-1')['state'] == 'waiting'
    assert ledger.get('request-1')['reason'] == 'merge_failed'
    assert cancel().status_code == 200


def test_wrong_target_dry_run_and_duplicate_request(world):
    _, ledger, _, fake, request, _, merge, post = world
    assert request().status_code == 200
    duplicate = post('change.request', {'issue_repo': 'owner/issues', 'kind': 'pr', 'number': 101, 'dry_run': False})
    assert duplicate.status_code == 409
    denied = post('change.request', {'issue_repo': 'other/repo', 'kind': 'pr', 'number': 7, 'dry_run': False})
    assert denied.status_code == 404
    dry = post('change.request', {'issue_repo': 'owner/issues', 'kind': 'pr', 'number': 102, 'dry_run': True}, 'dry')
    assert dry.status_code == 200
    assert ledger.get('dry')['dry_run'] == 1
    assert not fake.github.merges


def test_notification_is_not_authorization(world):
    _, ledger, _, fake, request, _, merge, _ = world
    assert request().status_code == 200
    fake.permissions.allowed = False
    assert merge()['status'] == 'waiting_user'
    assert ledger.get('request-1')['state'] == 'waiting'
    assert not fake.github.merges


def test_definitive_failure_can_be_retried(world):
    _, ledger, _, fake, request, _, merge, _ = world
    assert request().status_code == 200
    fake.github.on_merge = lambda *args: {'merged': False, 'definitive': True}
    assert merge()['reason'] == 'merge_failed'
    fake.github.on_merge = None
    assert merge()['reason'] == 'release_pending'
    assert ledger.get('request-1')['state'] == 'merged'
    assert len(fake.github.merges) == 1


def test_external_merge_and_trusted_pr_are_not_cancelled(world):
    _, ledger, _, fake, request, cancel, _, _ = world
    assert request().status_code == 200
    ledger.resolve_pr = lambda repo, number: {'issue_repo': repo, 'number': number, 'merged': False, 'untrusted': False}
    assert cancel().status_code == 403
    assert ledger.get('request-1')['state'] == 'waiting'
    ledger.resolve_pr = lambda repo, number: {'issue_repo': repo, 'number': number, 'merged': True, 'untrusted': True}
    assert cancel().status_code == 409
    assert ledger.get('request-1')['state'] == 'merged'
    assert not fake.github.merges


def test_changed_head_rejected_without_changing_ledger(world):
    _, ledger, _, fake, request, _, merge, _ = world
    assert request().status_code == 200
    fake.github.change_head()
    assert merge()['reason'] == 'pr_identity_mismatch'
    assert ledger.get('request-1')['state'] == 'waiting'
    assert not fake.github.merges


def test_pr_fetch_failure_keeps_waiting(world):
    _, ledger, _, fake, request, _, merge, _ = world
    assert request().status_code == 200
    def fail(*args):
        raise OSError('offline')
    # The helper reads once to prepare evidence; fail during the flow's preflight.
    fake.github.on_read = lambda *args: fail()
    it = {**item(), 'number': 101, 'pr': 101, 'request_id': 'request-1'}
    fake.github.on_read = None
    live = fake.github.view(it, target())
    safe = fake.safety.review({**it, 'snapshot': live})
    verified = fake.verification.verify(it, target(), head_sha=live['head_sha'], base_sha=live['base_sha'])
    fake.github.on_read = lambda *args: fail()
    journal = Journal(Path(ledger.path).parent / 'separate-journal.db', scrub=fake.ports.scrub, clock=Clock())
    journal.change_ledger = ChangeLedger(ledger.path, ['owner/issues'])
    try:
        flow = ChangeFlow(fake.ports, journal, [target()], clock=Clock())
        assert flow.merge(it, target(), safe, verified)['reason'] == 'pr_state_unavailable'
        assert ledger.get('request-1')['state'] == 'waiting'
        assert not fake.github.merges
    finally:
        journal.close()


def test_intake_and_merge_in_separate_processes(world):
    import multiprocessing
    _, ledger, _, fake, request, _, merge, _ = world
    ctx = multiprocessing.get_context('fork')
    results = ctx.Queue()
    def accept():
        results.put(request().status_code)
    process = ctx.Process(target=accept)
    process.start()
    process.join(15)
    assert process.exitcode == 0
    assert results.get(timeout=5) == 200
    assert ledger.get('request-1')['head_sha'] == HEAD
    assert merge()['reason'] == 'release_pending'
    assert ledger.get('request-1')['state'] == 'merged'
    assert len(fake.github.merges) == 1


def test_registered_change_endpoint_fails_soft_without_ledger(tmp_path):
    config = tmp_path / 'config'
    config.mkdir()
    entries = yaml.safe_load((ROOT / 'config/intake-destinations.d/b12b3-change.yaml').read_text())
    (config / 'intake-destinations.yaml').write_text(yaml.safe_dump(entries))
    app = build_app(config_dir=config, dest_dir=ROOT / 'core/intake/dest', ext_dir=tmp_path / 'empty',
                    idem=IdempotencyStore(None, retain_h=24), run_heartbeat_loop=False)
    async def authenticated(scope, receive, send):
        scope['ax_ident'] = Ident('local', 'commander', 'owner', 'operate')
        await app(scope, receive, send)
    try:
        with TestClient(authenticated) as client:
            response = client.post('/v1/instructions', json={
                'id': ids.new_ulid(), 'from': 'commander', 'destination': 'change.request',
                'payload': {'issue_repo': 'owner/issues', 'kind': 'pr', 'number': 101, 'dry_run': False},
                'idempotency_key': ids.new_ulid()})
            assert response.status_code == 503
            assert 'change_unavailable' in str(response.json()), response.json()
            assert client.get('/healthz').status_code == 200
    finally:
        app.state.intake.executor.shutdown()


def test_production_defaults_reject_change_without_ledger_or_delivery(tmp_path, monkeypatch):
    from plugins.subloop.change.integration.runtime import build_executor

    monkeypatch.setenv('AX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('AX_CONFIG_DIR', str(ROOT / 'config'))
    env = {'AX_PLUGIN_ID': 'change', 'AX_DATA_DIR': str(tmp_path / 'change'),
           'AX_HOST': 'test', 'AX_CHANGE_PORT_FACTORY': ''}
    executor, flow, errors = build_executor(root=ROOT, environ=env)
    app = build_app(run_heartbeat_loop=False)

    async def authenticated(scope, receive, send):
        scope['ax_ident'] = Ident('local', 'commander', 'owner', 'operate')
        await app(scope, receive, send)

    try:
        assert flow is None
        assert any(e['code'] == 'change_ports_unconfigured' for e in errors)
        assert not executor.graph.nodes
        assert app.state.intake.registry.get('change.request')['handler'] == 'b12b3:change.request'
        assert getattr(app.state, 'change_ledger', None) is None
        with TestClient(authenticated) as client:
            response = client.post('/v1/instructions', json={
                'id': ids.new_ulid(), 'from': 'commander', 'destination': 'change.request',
                'payload': {'issue_repo': 'owner/issues', 'kind': 'pr', 'number': 101, 'dry_run': False},
                'idempotency_key': ids.new_ulid()})
        assert response.status_code == 503
        assert response.json()['result']['error']['code'] == 'change_unavailable'
        assert not list(tmp_path.rglob('operations.db'))
        journal = executor.change_journal
        assert journal.db.execute("SELECT COUNT(*) FROM operation WHERE kind='delivery'").fetchone()[0] == 0
        assert journal.events() == []
    finally:
        app.state.intake.executor.shutdown()
        executor.change_journal.close()

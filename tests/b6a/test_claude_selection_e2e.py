"""Exercise public job submission with isolated resources and a fake launcher."""
import importlib
import time

import pytest
import yaml
from starlette.testclient import TestClient

from axcommon.creds import TokenTable, sha256_hex
from core.resources.app import Config as ResourceConfig, build as build_resources
from core.resources.core_auth import CoreAuth
from tests.b6a.test_dispatch_e2e import Control, ResourceAdapter, wait_for

SONNET = 'claude/claude-sonnet-5-5'
OPUS = 'claude/claude-opus-4-8'
GPT = 'codex/gpt-6.1-sol'
CHEAP = 'example/cheap'


def model(mid, family, tier='last_resort', level='L4'):
    return {'id': mid, 'kind': 'model', 'family': family, 'tier': tier,
            'billing': 'flat', 'level': level, 'vision': False,
            'runner': 'agent-cli', 'status': 'active'}


@pytest.fixture
def system_factory(tmp_path, monkeypatch):
    tokens = tmp_path / 'tokens'
    tokens.write_text('intake ' + sha256_hex('test-intake') + '\n')
    monkeypatch.setenv('AX_CORE_TOKENS', str(tokens))
    contexts = []

    def create(rows):
        models = tmp_path / 'models.yaml'
        models.write_text(yaml.safe_dump({'models': rows}))
        auth = CoreAuth(TokenTable({'worker-pool': sha256_hex('pool-test'),
                                    'control': sha256_hex('control-test')}))
        app, _, _ = build_resources(ResourceConfig(db=str(tmp_path / 'resources.sqlite'),
            models_file=str(models), worker_pool_db=str(tmp_path / 'pool.sqlite')),
            auth=auth, run_heartbeat_loop=False)
        resource = TestClient(app).__enter__()
        caps = {row['id']: 1 for row in rows}
        families = {row['family']: 1 for row in rows}
        assert resource.put('/v1/slots/effective-limits', headers={'Authorization': 'Bearer control-test'},
            json={'revision': 1, 'expires_at': time.time() + 3600, 'models': caps, 'families': families}).status_code == 200

        class Launcher:
            def __init__(self):
                self.calls = []
            def job_start(self, req):
                self.calls.append(req)
                return {'job_id': req['job_id']}
            def job_status(self, job_id):
                return {'state': 'running'}

        launcher = Launcher()
        events = []
        pool = importlib.import_module('plugins.resource.worker-pool.app')
        app, _, _ = pool.build(pool.Config(db=str(tmp_path / 'pool.sqlite')),
            resources=ResourceAdapter(resource), control=Control(), launcher=launcher,
            dispatch_interval_s=.05, run_heartbeat_loop=False,
            emit=lambda kind, subject, data, **kw: events.append((kind, subject, data)))
        client = TestClient(app, headers={'Authorization': 'Bearer test-intake'}).__enter__()
        contexts.append((client, resource))
        return client, launcher, events

    yield create
    for client, resource in contexts:
        client.__exit__(None, None, None)
        resource.__exit__(None, None, None)


def submit(client, role):
    response = client.post('/v1/jobs', json={'role': role, 'prompt': 'work', 'sensitivity': 'public'})
    assert response.status_code == 202
    return response.json()['job_id']


@pytest.mark.parametrize('role', ['backup_manager', 'test_design'])
def test_sonnet_allowed_for_designated_roles(system_factory, role):
    client, launcher, _ = system_factory([model(SONNET, 'claude'), model(OPUS, 'claude')])
    job = submit(client, role)
    wait_for(lambda: client.get('/v1/jobs/' + job).json()['state'] == 'running')
    assert [call['model'] for call in launcher.calls] == [SONNET]


@pytest.mark.parametrize('role', ['summary', 'impl_hard', 'review_final', 'manager'])
def test_claude_forbidden_for_other_roles(system_factory, role):
    client, launcher, events = system_factory([model(SONNET, 'claude'), model(OPUS, 'claude')])
    job = submit(client, role)
    wait_for(lambda: any(kind == 'worker.selection' and subject == job for kind, subject, _ in events))
    assert client.get('/v1/jobs/' + job).json()['state'] == 'queued'
    assert not launcher.calls
    selection = next(data for kind, subject, data in events if kind == 'worker.selection' and subject == job)
    assert selection['selected'] is None


def test_manager_accepts_free_l1(system_factory):
    client, launcher, _ = system_factory([model(CHEAP, 'free', 'free', 'L1')])
    job = submit(client, 'manager')
    wait_for(lambda: client.get('/v1/jobs/' + job).json()['state'] == 'running')
    assert launcher.calls[0]['model'] == CHEAP


@pytest.mark.parametrize(('role', 'runs'), [('impl_hard', True), ('impl', False)])
def test_gpt_existing_hard_use_condition(system_factory, role, runs):
    client, launcher, events = system_factory([model(GPT, 'gpt')])
    job = submit(client, role)
    wait_for(lambda: any(kind == 'worker.selection' and subject == job for kind, subject, _ in events))
    if runs:
        wait_for(lambda: client.get('/v1/jobs/' + job).json()['state'] == 'running')
        assert launcher.calls[0]['model'] == GPT
    else:
        assert client.get('/v1/jobs/' + job).json()['state'] == 'queued'
        assert not launcher.calls


def test_capacity_alone_does_not_trigger_sonnet(system_factory):
    client, launcher, events = system_factory([model(CHEAP, 'glm', 'standard', 'L3'), model(SONNET, 'claude')])
    first = submit(client, 'test_design')
    wait_for(lambda: client.get('/v1/jobs/' + first).json()['state'] == 'running')
    second = submit(client, 'test_design')
    wait_for(lambda: any(kind == 'worker.selection' and subject == second for kind, subject, _ in events))
    time.sleep(.3)
    assert launcher.calls[0]['model'] == CHEAP
    assert all(call['model'] != SONNET for call in launcher.calls)
    assert client.get('/v1/jobs/' + second).json()['state'] == 'queued'


@pytest.mark.parametrize('role', ['test_design', 'backup_manager', 'impl_hard'])
def test_opus_alone_never_runs(system_factory, role):
    client, launcher, events = system_factory([model(OPUS, 'claude')])
    job = submit(client, role)
    wait_for(lambda: any(kind == 'worker.selection' and subject == job for kind, subject, _ in events))
    assert client.get('/v1/jobs/' + job).json()['state'] == 'queued'
    assert not launcher.calls


def test_capacity_tries_other_standard_model(system_factory):
    other = 'example/other'
    client, launcher, _ = system_factory([model(CHEAP, 'glm', 'standard', 'L3'),
                                          model(other, 'other', 'standard', 'L3'),
                                          model(SONNET, 'claude')])
    first = submit(client, 'test_design')
    wait_for(lambda: client.get('/v1/jobs/' + first).json()['state'] == 'running')
    second = submit(client, 'test_design')
    wait_for(lambda: client.get('/v1/jobs/' + second).json()['state'] == 'running')
    assert [call['model'] for call in launcher.calls] == [CHEAP, other]


def test_unavailable_standard_allows_last_resort(system_factory):
    unavailable = {**model(CHEAP, 'glm', 'standard', 'L3'), 'status': 'excluded'}
    client, launcher, _ = system_factory([unavailable, model(SONNET, 'claude')])
    jid = submit(client, 'test_design')
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'running')
    assert launcher.calls[0]['model'] == SONNET

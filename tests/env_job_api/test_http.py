import hashlib
import threading
import time
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient

from axcommon.creds import TokenTable
from core.resources.app import Config, build
from core.resources.envs.jobs import Jobs
from plugins.timer.b16a_monitor.environment_jobs import EnvironmentJobsClient


@pytest.fixture
def http(tmp_path):
    tokens = TokenTable({'monitor': hashlib.sha256(b'test-token').hexdigest(),
                         'other': hashlib.sha256(b'other-token').hexdigest()})
    manifest = {'id': 'monitor', 'secrets': [], 'env_jobs': {
        'operations': ['submit', 'get'], 'entrypoints': ['audit-readonly'],
        'requires': ['security-audit']}}
    envs = [dict(id=name, state='available', capabilities=['security-audit'],
                 data_classes=['private'], max_concurrent=1, entrypoints=['audit-readonly'])
            for name in ('host-a', 'host-b')]
    def execute(env, body):
        if env['id'] == 'host-b':
            return {'status': 'error', 'error': 'execution_failed'}
        return {'status': 'ok', 'values': {'test': True}}
    jobs = Jobs(tokens=tokens, manifests=lambda owner: manifest if owner == 'monitor' else None,
                environments=lambda: envs, executor=execute)
    _, app, _ = build(Config(db=str(tmp_path / 'resources.sqlite')), env_jobs=jobs, run_heartbeat_loop=False)
    with TestClient(app) as client:
        yield client


def request_body():
    return {'job_id': 'test-job', 'requires': ['security-audit'], 'scope': 'each',
            'sensitivity': 'private', 'entry': {'kind': 'entrypoint', 'name': 'audit-readonly', 'args': {}},
            'secrets': [], 'outputs': []}


def test_auth_and_permissions(http):
    assert http.post('/v1/env/jobs', json=request_body()).status_code == 401
    assert http.get('/v1/env/jobs/missing').status_code == 401
    assert http.post('/v1/env/jobs', headers={'Authorization': 'Bearer other-token'}, json=request_body()).status_code == 403
    body = request_body()
    body['entry']['name'] = 'unapproved'
    assert http.post('/v1/env/jobs', headers={'Authorization': 'Bearer test-token'}, json=body).status_code == 403
    assert http.get('/v1/env/jobs/missing', headers={'Authorization': 'Bearer test-token'}).status_code == 404


def test_client_roundtrip_and_partial_failure(http):
    def transport(method, url, headers, body, timeout):
        response = http.request(method, urlsplit(url).path, headers=headers, content=body)
        return response.status_code, dict(response.headers), response.content
    client = EnvironmentJobsClient('http://resources:8081', 'test-token', transport=transport)
    results = client.run(requires=['security-audit'], scope='each', entry='audit-readonly')
    assert results['host-a'] == {'status': 'ok', 'values': {'test': True}}
    assert results['host-b'] == {'status': 'error', 'error': 'execution_failed'}


def test_single_selection_duplicate_and_owner_isolation(http):
    headers = {'Authorization': 'Bearer test-token'}
    body = request_body()
    body['scope'] = 'one'
    assert http.post('/v1/env/jobs', headers=headers, json=body).status_code == 202
    response = wait_for(http, 'test-job', lambda j: j['state'] == 'succeeded')
    assert response.status_code == 200
    assert list(response.json()['results']) == ['host-a']
    assert http.post('/v1/env/jobs', headers=headers, json=body).status_code == 409
    assert http.get('/v1/env/jobs/test-job', headers={'Authorization': 'Bearer other-token'}).status_code == 403


def wait_for(http, job_id, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = http.get('/v1/env/jobs/' + job_id, headers={'Authorization': 'Bearer test-token'})
        if response.status_code == 200 and predicate(response.json()):
            return response
        time.sleep(0.02)
    raise AssertionError(f'job did not reach expected state: {response.json()}')


def test_restart_recovery_and_capacity(tmp_path):
    tokens = TokenTable({'monitor': hashlib.sha256(b'test-token').hexdigest()})
    manifest = {'id': 'monitor', 'secrets': [], 'env_jobs': {
        'operations': ['submit', 'get'], 'entrypoints': ['audit-readonly'], 'requires': ['security-audit']}}
    envs = [dict(id=name, state='available', capabilities=['security-audit'], data_classes=['private'],
                 max_concurrent=1, entrypoints=['audit-readonly']) for name in ('host-a', 'host-b', 'host-c')]
    entered = {'host-a': threading.Event(), 'host-b': threading.Event()}
    release = threading.Event()
    class Executor:
        def __init__(self):
            self.calls = []
            self.lookups = []
        def submit(self, env, spec):
            self.calls.append((env['id'], spec['job_id']))
            if spec['job_id'] == 'first' and env['id'] in entered:
                entered[env['id']].set()
                release.wait(10)
            return {'status': 'ok', 'values': {}}
        def status(self, env, spec):
            self.lookups.append((env['id'], spec['job_id']))
            if env['id'] == 'host-a':
                return {'status': 'ok', 'values': {'recovered': True}}
            raise OSError('offline')
    executor = Executor()
    db = str(tmp_path / 'persistent.sqlite')
    def app():
        jobs = Jobs(tokens=tokens, manifests=lambda _: manifest, environments=lambda: envs, executor=executor)
        return build(Config(db=db), env_jobs=jobs, run_heartbeat_loop=False)[1]
    headers = {'Authorization': 'Bearer test-token'}
    try:
        with TestClient(app()) as http:
            first = request_body(); first['job_id'] = 'first'
            second = request_body(); second['job_id'] = 'second'
            assert http.post('/v1/env/jobs', headers=headers, json=first).status_code == 202
            assert all(event.wait(3) for event in entered.values())
            assert http.post('/v1/env/jobs', headers=headers, json=second).status_code == 202
            result = wait_for(http, 'second', lambda j: j['results']['host-c']['status'] == 'ok').json()
            assert result['results']['host-a']['status'] == 'queued'
            assert result['results']['host-b']['status'] == 'queued'
        with TestClient(app()) as http:
            result = wait_for(http, 'first', lambda j: j['results']['host-b'].get('error') == 'status_unknown').json()
            assert result['results']['host-a']['values'] == {'recovered': True}
            assert result['results']['host-b']['error'] == 'status_unknown'
            result = wait_for(http, 'second', lambda j: j['results']['host-a']['status'] == 'ok').json()
            assert result['results']['host-b']['status'] == 'queued'
            assert executor.calls.count(('host-a', 'first')) == 1
            assert executor.calls.count(('host-b', 'first')) == 1
            assert ('host-b', 'second') not in executor.calls
            assert set(executor.lookups) == {('host-a', 'first'), ('host-b', 'first')}
    finally:
        release.set()


def test_unknown_ssh_job_retains_slot_until_periodic_confirmation(tmp_path):
    import json
    from types import SimpleNamespace
    from core.resources.envs.adapters.ssh_host import SSHHostAdapter

    tokens = TokenTable({'monitor': hashlib.sha256(b'test-token').hexdigest()})
    manifest = {'id': 'monitor', 'secrets': [], 'env_jobs': {
        'operations': ['submit', 'get'], 'entrypoints': ['audit-readonly'], 'requires': ['security-audit']}}
    environment = {'id': 'host-a', 'state': 'available', 'adapter': 'ssh-host',
                   'connect': {'kind': 'ssh', 'host': 'host', 'user': 'user', 'port': 22},
                   'conn_credential': {'slot': 'env-ssh-host-a', 'place': 'secrets_dir/env-ssh-host-a'},
                   'secret_targets': [], 'capabilities': ['security-audit'], 'data_classes': ['private'],
                   'max_concurrent': 1, 'entrypoints': ['audit-readonly']}
    completed = threading.Event()
    calls = []
    def runner(argv, **kwargs):
        operation, job_id = argv[-2:]
        calls.append((operation, job_id))
        if operation == 'submit' and job_id == 'first':
            raise OSError('connection lost')
        state = 'succeeded' if job_id == 'second' or completed.is_set() else 'unknown'
        return SimpleNamespace(returncode=0, stdout=json.dumps({'state': state}).encode())
    adapter = SSHHostAdapter.from_environment(environment, secrets_dir=str(tmp_path), runner=runner)
    class Executor:
        def submit(self, env, spec):
            response = adapter.submit(spec)
            if response['state'] != 'succeeded':
                raise OSError('uncertain')
            return {'status': 'ok', 'values': {}}
        def status(self, env, spec):
            response = adapter.status(spec['job_id'])
            if response['state'] != 'succeeded':
                raise OSError('uncertain')
            return {'status': 'ok', 'values': {'confirmed': True}}
    jobs = Jobs(tokens=tokens, manifests=lambda _: manifest, environments=lambda: [environment],
                executor=Executor(), reconcile_interval=0.05)
    _, app, _ = build(Config(db=str(tmp_path / 'jobs.sqlite')), env_jobs=jobs, run_heartbeat_loop=False)
    headers = {'Authorization': 'Bearer test-token'}
    with TestClient(app) as client:
        first = request_body(); first['job_id'] = 'first'; first['idempotency_key'] = 'first'
        second = request_body(); second['job_id'] = 'second'; second['idempotency_key'] = 'second'
        assert client.post('/v1/env/jobs', headers=headers, json=first).status_code == 202
        wait_for(client, 'first', lambda result: result['state'] == 'unknown')
        assert client.post('/v1/env/jobs', headers=headers, json=second).status_code == 202
        wait_for(client, 'first', lambda result: ('status', 'first') in calls)
        assert client.get('/v1/env/jobs/second', headers=headers).json()['state'] == 'queued'
        assert calls.count(('submit', 'first')) == 1 and ('submit', 'second') not in calls
        completed.set()
        wait_for(client, 'first', lambda result: result['state'] == 'succeeded')
        wait_for(client, 'second', lambda result: result['state'] == 'succeeded')
        assert calls.count(('submit', 'first')) == 1


@pytest.mark.parametrize("configured", ["gate", "mcp", "both"])
def test_shared_capacity_guard_keeps_environment_jobs_available(tmp_path, configured):
    tokens = TokenTable({'monitor': hashlib.sha256(b'test-token').hexdigest()})
    manifest = {'id': 'monitor', 'secrets': [], 'env_jobs': {
        'operations': ['submit', 'get'], 'entrypoints': ['audit-readonly'],
        'requires': ['security-audit']}}
    envs = [dict(id='host-a', state='available', capabilities=['security-audit'],
                 data_classes=['private'], max_concurrent=1, entrypoints=['audit-readonly'])]
    jobs = Jobs(tokens=tokens, manifests=lambda owner: manifest if owner == 'monitor' else None,
                environments=lambda: envs, executor=lambda env, body: {'status': 'ok', 'values': {'test': True}})
    from core.resources.envs.sandbox.admission import PolicyGate
    from core.resources.envs.sandbox.mcp import SandboxMCP
    def forbidden(*args, **kwargs):
        pytest.fail('sandbox dependency was called')
    gate = object()
    policy = object()
    wrapped = PolicyGate(gate, policy=policy, resolve_job=forbidden, repo_is_public=forbidden)
    mcp = SandboxMCP(gate=gate, policy=policy, resolve_job=forbidden, repo_is_public=forbidden,
                     implementations={}, invoke=forbidden)
    core, work, _ = build(Config(db=str(tmp_path / 'resources.sqlite')), env_jobs=jobs,
                          b18_gate=wrapped if configured != 'mcp' else None,
                          b18_mcp=mcp if configured != 'gate' else None, run_heartbeat_loop=False)
    with TestClient(core) as core_client, TestClient(work) as work_client:
        for client, path in ((core_client, '/v1/envs/box/gate-token'), (work_client, '/mcp/box')):
            response = client.post(path, json={})
            assert response.status_code == 503
            assert response.json()['error']['code'] == 'shared_capacity_unavailable'
            assert client.get('/healthz').json()['b18_status'] == 'shared_capacity_unavailable'
        assert work_client.post('/v1/env/jobs', headers={'Authorization': 'Bearer test-token'}, json=request_body()).status_code == 202
        assert wait_for(work_client, 'test-job', lambda j: j['state'] == 'succeeded').status_code == 200
        assert work_client.get('/status').status_code == 200


def test_unconfigured_b18_is_not_enabled(tmp_path):
    core, work, _ = build(Config(db=str(tmp_path / 'resources.sqlite')), run_heartbeat_loop=False)
    with TestClient(core) as core_client, TestClient(work) as work_client:
        assert core_client.get('/healthz').json()['b18_status'] == 'not_configured'
        assert work_client.get('/healthz').json()['b18_status'] == 'not_configured'
        assert core_client.post('/v1/envs/box/gate-token').status_code == 404
        assert work_client.post('/mcp/box').status_code == 404

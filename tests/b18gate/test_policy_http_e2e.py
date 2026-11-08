"""HTTP admission tests using a fake job registry and no provider connection."""
import hashlib

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from axcommon.redact import Redactor
from core.resources.envs.sandbox.admission import PolicyGate
from core.resources.envs.sandbox.free_guard import FreeMeasurement
from core.resources.envs.sandbox.http import build_routes
from core.resources.envs.sandbox.probe_common import ProviderState, apply_gate_key
from core.resources.envs.sandbox.sqlite_gate import SQLiteGate
from core.resources.envs.sandbox_policy import SandboxPolicy

AUTH = '1' * 64
SECRET = 'fixture-private-person'


class Provider:
    capabilities = {'linux-sandbox'}
    def read_worker_key(self):
        return b'fake-worker-key'
    def measure(self):
        return FreeMeasurement(1, 1, 0, 100, 100, 0)


@pytest.mark.parametrize('case,code', [
    ('allowed', None), ('unknown', 'tool_denied'),
    ('credential', 'credential_missing'), ('private', 'repo_not_public'),
    ('unknown_visibility', 'repo_not_public'), ('secret', 'scan_blocked'),
])
def test_admission(tmp_path, caplog, case, code):
    index = tmp_path / 'index'
    index.write_text('fixture-salt\n')
    pii = tmp_path / 'pii'
    pii.write_text(SECRET + '\n')
    policy = SandboxPolicy(caller='gate', secrets_dir=tmp_path,
                           redactor=Redactor(index_path=index, pii_list_path=pii))
    gate = SQLiteGate(tmp_path / 'gate.sqlite', auth_digest=hashlib.sha256(AUTH.encode()).hexdigest(),
                      providers={'box': Provider()}, clock=lambda: 1000)
    state = ProviderState('box')
    state.record_ax_probe(True)
    state.complete_j_trial(success=True, trial_for='box')
    apply_gate_key(state, 'ok', 1000)
    state.complete_g_trial(success=True, trial_for='box', now=1000, max_age_s=300)
    gate.save_state(state)
    repo = 'example/private' if case == 'private' else 'example/unknown' if case == 'unknown_visibility' else 'example/public'
    context = {'environment': {'work_credentials': [{'slot': 'identity', 'place': '/workspace/identity'}]},
               'required_credentials': ('identity',), 'root': '/workspace/ax',
               'spec': {'sensitivity': 'public', 'secrets': [] if case == 'credential' else ['identity'],
                        'inputs': [{'ref': f'git:{repo}@abc'}]},
               'operations': [{'name': 'unknown' if case == 'unknown' else 'shell',
                               'args': {'command': SECRET if case == 'secret' else 'true'}}]}
    wrapped = PolicyGate(gate, policy=policy, resolve_job=lambda env, job: context,
                         repo_is_public=lambda name: {'example/public': True, 'example/private': False}.get(name))
    payload = {'job_id': 'job1', 'role': 'research', 'sensitivity': 'public',
               'tools': ['linux-sandbox'], 'budget_min': 20}
    with TestClient(Starlette(routes=build_routes(wrapped))) as client:
        response = client.post('/v1/envs/box/gate-token', headers={'Authorization': 'Bearer ' + AUTH}, json=payload)
    assert response.status_code == (201 if code is None else 403)
    if code:
        assert response.json()['error']['code'] == code
    assert SECRET not in response.text
    assert SECRET not in caplog.text


def test_cancel_persists_across_gate_restart_and_isolates_other_box(tmp_path, monkeypatch):
    import threading
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from core.resources.envs.adapters.cloud_sandbox import CloudSandboxAdapter
    from core.resources.envs.sandbox.instabox import InstaboxCancelled

    index, pii = tmp_path / 'index2', tmp_path / 'pii2'
    index.write_text('fixture-salt\n')
    pii.write_text('fixture-private-person\n')
    redactor = Redactor(index_path=index, pii_list_path=pii)
    monkeypatch.setattr('core.resources.envs.adapters.cloud_sandbox.from_environment', lambda: redactor)
    started = threading.Event()

    class Driver:
        executable = str(tmp_path / 'fake-driver')
        def execute(self, request, cancel_event):
            started.set()
            cancel_event.wait(5)
            raise InstaboxCancelled('cancelled')
        def status(self):
            return {}

    path = tmp_path / 'restart.sqlite'
    def create_gate():
        return SQLiteGate(path, auth_digest=hashlib.sha256(AUTH.encode()).hexdigest(),
                          providers={'box': Provider(), 'other': Provider()}, clock=lambda: 1000)
    gate = create_gate()
    for env_id in ('box', 'other'):
        state = ProviderState(env_id)
        state.record_ax_probe(True)
        state.complete_j_trial(success=True, trial_for=env_id)
        apply_gate_key(state, 'ok', 1000)
        state.complete_g_trial(success=True, trial_for=env_id, now=1000, max_age_s=300)
        gate.save_state(state)
    adapter = CloudSandboxAdapter(driver=Driver(), remote_id='box', state_store=gate)
    job = {'job_id': 'job1', 'idempotency_key': 'job1', 'sensitivity': 'public',
           'entry': {'command': 'true'}}
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(adapter.submit(job)))
    thread.start()
    assert started.wait(5)
    async def cancel(request):
        return JSONResponse(adapter.cancel('job1'))
    with TestClient(Starlette(routes=[Route('/cancel', cancel, methods=['POST'])])) as client:
        assert client.post('/cancel').status_code == 200
    thread.join(5)
    assert not thread.is_alive()
    assert outcome[0]['remote_state'] == 'unknown'
    restarted = create_gate()
    payload = {'job_id': 'next', 'role': 'research', 'sensitivity': 'public',
               'tools': ['linux-sandbox'], 'budget_min': 20}
    headers = {'Authorization': 'Bearer ' + AUTH}
    with TestClient(Starlette(routes=build_routes(restarted))) as client:
        blocked = client.post('/v1/envs/box/gate-token', headers=headers, json=payload)
        allowed = client.post('/v1/envs/other/gate-token', headers=headers, json=payload)
    assert blocked.status_code == 403
    assert blocked.json()['error']['code'] == 'remote_state_unknown'
    assert allowed.status_code == 201


def test_mcp_http_rechecks_job_and_each_operation(tmp_path):
    from core.resources.envs.sandbox.mcp import SandboxMCP, build_mcp_routes

    index, pii = tmp_path / 'mcp-index', tmp_path / 'mcp-pii'
    index.write_text('fixture-salt\n')
    pii.write_text(SECRET + '\n')
    policy = SandboxPolicy(caller='gate', secrets_dir=tmp_path,
                           redactor=Redactor(index_path=index, pii_list_path=pii))
    path = tmp_path / 'mcp.sqlite'
    gate = SQLiteGate(path, auth_digest=hashlib.sha256(AUTH.encode()).hexdigest(),
                      providers={'box': Provider()}, clock=lambda: 1000)
    state = ProviderState('box')
    state.record_ax_probe(True)
    state.complete_j_trial(success=True, trial_for='box')
    apply_gate_key(state, 'ok', 1000)
    state.complete_g_trial(success=True, trial_for='box', now=1000, max_age_s=300)
    gate.save_state(state)
    context = {'environment': {'work_credentials': [{'slot': 'identity', 'place': '/workspace/identity'}]},
               'required_credentials': ('identity',), 'root': '/workspace/ax',
               'spec': {'sensitivity': 'public', 'secrets': ['identity'],
                        'inputs': [{'ref': 'git:example/public@abc'}]},
               'operations': [{'name': 'shell', 'args': {'command': 'true'}}]}
    resolve = lambda env, job: context
    visibility = lambda repo: True if repo == 'example/public' else None
    sent = []
    wrapped = PolicyGate(gate, policy=policy, resolve_job=resolve, repo_is_public=visibility)
    mcp = SandboxMCP(gate=gate, policy=policy, resolve_job=resolve, repo_is_public=visibility,
                     implementations={'box': {'shell'}},
                     invoke=lambda env, mapping, args: sent.append(args) or {'ok': True})
    payload = {'job_id': 'job1', 'role': 'research', 'sensitivity': 'public',
               'tools': ['linux-sandbox'], 'budget_min': 20}
    with TestClient(Starlette(routes=build_routes(wrapped) + build_mcp_routes(mcp))) as client:
        issued = client.post('/v1/envs/box/gate-token', headers={'Authorization': 'Bearer ' + AUTH}, json=payload)
        assert issued.status_code == 201
        headers = {'Authorization': 'Bearer ' + issued.json()['token']}
        def call(name, args):
            return client.post('/mcp/box', headers=headers, json={'jsonrpc': '2.0', 'id': 1,
                               'method': 'tools/call', 'params': {'name': name, 'arguments': args}}).json()
        listing = client.post('/mcp/box', headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}).json()
        assert [item['name'] for item in listing['result']['tools']] == ['shell']
        mcp.gate = SQLiteGate(path, auth_digest=hashlib.sha256(AUTH.encode()).hexdigest(),
                              providers={'box': Provider()}, clock=lambda: 1000)
        assert 'result' in call('shell', {'command': 'true'})
        assert sent == [{'command': 'true'}]
        assert 'error' in call('unknown_operation', {})
        assert 'error' in call('shell', {'command': SECRET})
        context['spec']['secrets'] = []
        assert 'error' in call('shell', {'command': 'true'})
        context['spec']['secrets'] = ['identity']
        context['spec']['inputs'] = [{'ref': 'git:example/unknown@abc'}]
        assert 'error' in call('shell', {'command': 'true'})
        assert sent == [{'command': 'true'}]
        context['spec']['inputs'] = [{'ref': 'git:example/public@abc'}]
        gate.mark_remote_unknown('box')
        assert client.post('/mcp/box', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).status_code == 401


def test_resources_app_gate_and_mcp_enforce_policy_after_restart(tmp_path, monkeypatch):
    from core.resources.app import build, Config
    from core.resources.envs.sandbox.mcp import SandboxMCP

    index, pii = tmp_path / 'app-index', tmp_path / 'app-pii'
    index.write_text('fixture-salt\n')
    pii.write_text(SECRET + '\n')
    policy = SandboxPolicy(caller='gate', secrets_dir=tmp_path,
                           redactor=Redactor(index_path=index, pii_list_path=pii))
    gate_path = tmp_path / 'sandbox.sqlite'
    def make_gate():
        return SQLiteGate(gate_path, auth_digest=hashlib.sha256(AUTH.encode()).hexdigest(),
                          providers={'box': Provider()}, clock=lambda: 1000)
    gate = make_gate()
    state = ProviderState('box')
    state.record_ax_probe(True)
    state.complete_j_trial(success=True, trial_for='box')
    apply_gate_key(state, 'ok', 1000)
    state.complete_g_trial(success=True, trial_for='box', now=1000, max_age_s=300)
    gate.save_state(state)
    context = {'environment': {'work_credentials': [{'slot': 'identity', 'place': '/workspace/identity'}]},
               'required_credentials': ('identity',), 'root': '/workspace/ax',
               'spec': {'sensitivity': 'public', 'secrets': ['identity'],
                        'inputs': [{'ref': 'git:example/public@abc'}]},
               'operations': [{'name': 'shell', 'args': {'command': 'true'}}]}
    resolve = lambda env, job: context
    visibility = lambda repo: repo == 'example/public'
    invoked = []
    def apps(active_gate):
        wrapped = PolicyGate(active_gate, policy=policy, resolve_job=resolve, repo_is_public=visibility)
        mcp = SandboxMCP(gate=active_gate, policy=policy, resolve_job=resolve,
                         repo_is_public=visibility, implementations={'box': {'shell'}},
                         invoke=lambda env, mapping, args: invoked.append(args) or {'ok': True})
        # Exercise the policy components independently of the resources startup guard.
        from starlette.applications import Starlette
        from core.resources.envs.sandbox.http import build_routes
        from core.resources.envs.sandbox.mcp import build_mcp_routes
        return Starlette(routes=build_routes(wrapped)), Starlette(routes=build_mcp_routes(mcp))
    payload = {'job_id': 'job1', 'role': 'research', 'sensitivity': 'public',
               'tools': ['linux-sandbox'], 'budget_min': 20}
    core, work = apps(gate)
    with TestClient(core) as core_client, TestClient(work) as work_client:
        issued = core_client.post('/v1/envs/box/gate-token', headers={'Authorization': 'Bearer ' + AUTH}, json=payload)
        assert issued.status_code == 201
        headers = {'Authorization': 'Bearer ' + issued.json()['token']}
        def call(name, args):
            return work_client.post('/mcp/box', headers=headers, json={'jsonrpc': '2.0', 'id': 1,
                'method': 'tools/call', 'params': {'name': name, 'arguments': args}}).json()
        assert 'result' in call('shell', {'command': 'true'})
        assert 'error' in call('unknown_operation', {})
        context['spec']['secrets'] = []
        assert 'error' in call('shell', {'command': 'true'})
        assert invoked == [{'command': 'true'}]
    context['spec']['secrets'] = ['identity']
    import threading
    from core.resources.envs.adapters.cloud_sandbox import CloudSandboxAdapter
    from core.resources.envs.sandbox.instabox import InstaboxCancelled
    monkeypatch.setattr('core.resources.envs.adapters.cloud_sandbox.from_environment', lambda: Redactor(index_path=index, pii_list_path=pii))
    started = threading.Event()
    class Driver:
        executable = str(tmp_path / 'fake-driver')
        def execute(self, request, cancel_event):
            started.set()
            cancel_event.wait(5)
            raise InstaboxCancelled('cancelled')
        def status(self):
            return {}
    adapter = CloudSandboxAdapter(driver=Driver(), remote_id='box', state_store=gate)
    result = []
    job = {'job_id': 'job1', 'idempotency_key': 'job1', 'sensitivity': 'public',
           'entry': {'command': 'true'}}
    thread = threading.Thread(target=lambda: result.append(adapter.submit(job)))
    thread.start()
    assert started.wait(5)
    adapter.cancel('job1')
    thread.join(5)
    assert not thread.is_alive() and result[0]['remote_state'] == 'unknown'
    restarted = make_gate()
    core, _ = apps(restarted)
    with TestClient(core) as client:
        response = client.post('/v1/envs/box/gate-token', headers={'Authorization': 'Bearer ' + AUTH}, json=payload)
        assert response.status_code == 403 and response.json()['error']['code'] == 'remote_state_unknown'

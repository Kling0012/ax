"""Fixed change profiles and idempotent public job submissions."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.b6a.test_dispatch_e2e import system, wait_for


@pytest.fixture(autouse=True)
def registered_chat_runner(system, monkeypatch):
    from tests.b6a.test_dispatch_e2e import LauncherAdapter
    _, resources, state = system
    models = resources.get('/v1/resources').json()['resources']
    for model in models:
        default = model.get('runner') or 'agent-cli'
        runners = list(dict.fromkeys([default, 'chat-json']))
        assert resources.put('/v1/model-runners', headers={'Authorization': 'Bearer control-test'},
                             json={'model': model['id'], 'runners': runners}).status_code == 200
    def evidence(jid):
        return {'job_id': jid, 'runner': 'chat-json', 'profile': 'tool-free-chat-v1', 'request_tools_absent': True, 'inspect_verified': True, 'trust_basis': 'host_managed_runner_and_image'}
    def chat(self, jid):
        doc = json.loads(state['report_log'])
        if doc.get('type') == 'result':
            if doc.get('is_error') is not False or doc.get('subtype') != 'success':
                return {'job_id': jid, 'tool_free': True, 'tool_free_evidence': evidence(jid), 'ok': False, 'exit_code': 1}
            doc = {'ok': True, 'exit_code': 0, 'json': json.loads(doc['result'])}
        return {'job_id': jid, 'tool_free': True, 'tool_free_evidence': evidence(jid), **doc}
    monkeypatch.setattr(LauncherAdapter, 'job_chat_result', chat, raising=False)


def digest(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def complete(client, state, body, envelope):
    state['report_log'] = json.dumps(envelope)
    response = client.post('/v1/jobs', json=body)
    assert response.status_code == 202, response.text
    jid = response.json()['job_id']
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'running')
    state['jobs'][jid] = {'state': 'exited', 'exit_code': 0}
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] in ('ok', 'failed'))
    return client.get('/v1/jobs/' + jid).json()


def envelope(value):
    return {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': json.dumps(value)}


@pytest.mark.parametrize('system', ['families'], indirect=True)
def test_review_and_independent_screen_via_public_api(system):
    client, _, state = system
    review_body = {'role': 'second_opinion', 'prompt': 'review data', 'sensitivity': 'public',
                   'verify': 'ax:change-safety-review-v1', 'idempotency_key': 'review-1'}
    review_value = {'verdict': 'safe', 'concerns': [], 'summary': 'A short neutral summary.'}
    review = complete(client, state, review_body, envelope(review_value))
    assert review['state'] == 'ok' and review['verify']['rc'] == 0
    assert json.loads(review['report']['text']) == review_value
    assert review['request_digest'] == digest(review_body)
    assert review['verify_profile'] == review_body['verify']
    screen_body = {'role': 'screen', 'prompt': review_value['summary'], 'sensitivity': 'public',
                   'verify': 'ax:change-injection-screen-v1', 'prior_job_id': review['id'],
                   'idempotency_key': 'screen-1'}
    screen = complete(client, state, screen_body, {'ok': True, 'exit_code': 0, 'json': {'safe': True}})
    assert screen['state'] == 'ok' and screen['verify']['rc'] == 0
    assert screen['family'] != review['family'] and screen['prior_job_id'] == review['id']
    assert screen['request_digest'] == digest(screen_body)
    assert json.loads(screen['report']['text']) == {'safe': True}


@pytest.mark.parametrize('case', ['bad_json', 'bad_schema', 'error', 'extra'])
def test_review_rejects_incomplete_or_error_outputs(system, case):
    client, _, state = system
    obj = {'verdict': 'safe', 'concerns': [], 'summary': 'neutral'}
    if case == 'bad_schema': obj['concerns'] = 'none'
    if case == 'extra': obj['unexpected'] = 1
    result = envelope(obj)
    if case == 'bad_json': result['result'] = '{'
    if case == 'error': result['is_error'] = True
    body = {'role': 'second_opinion', 'prompt': 'review', 'sensitivity': 'public', 'verify': 'ax:change-safety-review-v1'}
    result = complete(client, state, body, result)
    assert result['state'] == 'failed' and result['verify']['rc'] != 0


def test_submit_key_is_atomic_and_conflicts_are_rejected(system):
    client, _, state = system
    body = {'role': 'summary', 'prompt': 'work', 'sensitivity': 'public', 'idempotency_key': 'same-1'}
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: client.post('/v1/jobs', json=body), range(6)))
    assert all(r.status_code == 202 for r in results)
    ids = {r.json()['job_id'] for r in results}
    assert len(ids) == 1
    jid = ids.pop()
    assert client.get('/v1/jobs/' + jid).json()['request_digest'] == digest(body)
    assert client.post('/v1/jobs', json={**body, 'prompt': 'different'}).status_code == 409
    created = [event for event in state.get('events', []) if event[0] == 'job.state' and event[2].get('job_id') == jid and event[2].get('state') == 'queued']
    assert len(created) == 1


def test_fixed_profile_role_and_screen_prior_are_required(system):
    client, _, _ = system
    base = {'prompt': 'work', 'sensitivity': 'public'}
    assert client.post('/v1/jobs', json={**base, 'role': 'summary', 'verify': 'ax:change-safety-review-v1'}).status_code == 422
    assert client.post('/v1/jobs', json={**base, 'role': 'screen', 'verify': 'ax:change-injection-screen-v1'}).status_code == 422

@pytest.mark.parametrize('system', ['families'], indirect=True)
@pytest.mark.parametrize('value', [{'safe': 'true'}, {'safe': True, 'extra': 1}, {}, None])
def test_screen_invalid_schema_fails_closed(system, value):
    client, _, state = system
    review = complete(client, state, {'role': 'second_opinion', 'prompt': 'review', 'sensitivity': 'public',
        'verify': 'ax:change-safety-review-v1'}, envelope({'verdict': 'safe', 'concerns': [], 'summary': 'summary'}))
    body = {'role': 'screen', 'prompt': 'summary', 'sensitivity': 'public',
            'verify': 'ax:change-injection-screen-v1', 'prior_job_id': review['id']}
    result = complete(client, state, body, envelope(value))
    assert result['state'] == 'failed' and result['verify']['rc'] == 1
    assert client.post('/v1/jobs', json={**body, 'project': 'other'}).status_code == 422


def test_idempotency_is_shared_between_pool_connections(system):
    import importlib
    from starlette.testclient import TestClient
    from tests.b6a.test_dispatch_e2e import Control, ResourceAdapter

    client, resources, _ = system
    service = client.app.state.service
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    core, _, second_service = pool.build(pool.Config(db=service.store.path), resources=ResourceAdapter(resources),
        control=Control(), run_heartbeat_loop=False, emit=lambda *args, **kwargs: None)
    second = TestClient(core, headers={'Authorization': 'Bearer test-intake'})
    body = {'role': 'summary', 'prompt': 'one logical job', 'sensitivity': 'public', 'idempotency_key': 'shared-connection'}
    with ThreadPoolExecutor(max_workers=2) as executor:
        replies = list(executor.map(lambda c: c.post('/v1/jobs', json=body), [client, second]))
    assert all(r.status_code == 202 for r in replies)
    assert replies[0].json()['job_id'] == replies[1].json()['job_id']
    assert len([job for job in service.store.list(limit=100) if job['idempotency_key'] == 'shared-connection']) == 1
    second_service.store.close()

@pytest.mark.parametrize('system', ['chat'], indirect=True)
@pytest.mark.parametrize('available', [True, False])
def test_chat_result_uses_saved_runner_and_never_falls_back_to_logs(system, monkeypatch, available):
    from tests.b6a.test_dispatch_e2e import LauncherAdapter
    client, resources, state = system
    state['report_log'] = json.dumps(envelope({'verdict': 'safe', 'concerns': [], 'summary': 'bad fallback'}))
    def collect(self, jid):
        return {'job_id': jid, 'tool_free': True, 'tool_free_evidence': {'job_id': jid, 'runner': 'chat-json', 'profile': 'tool-free-chat-v1', 'request_tools_absent': True, 'inspect_verified': True, 'trust_basis': 'host_managed_runner_and_image'}, 'ok': True, 'exit_code': 0, 'json': {'verdict': 'safe', 'concerns': [], 'summary': 'chat response'}}
    if not available:
        def collect(self, jid):
            raise ValueError('unavailable')
    monkeypatch.setattr(LauncherAdapter, 'job_chat_result', collect, raising=False)
    body = {'role': 'second_opinion', 'prompt': 'review', 'sensitivity': 'public', 'verify': 'ax:change-safety-review-v1'}
    response = client.post('/v1/jobs', json=body)
    assert response.status_code == 202
    jid = response.json()['job_id']
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'running')
    with resources.app.state.service.store._lock:
        resources.app.state.service.store._conn.execute("UPDATE resources SET runner='agent-cli' WHERE id='example/model'")
    state['jobs'][jid] = {'state': 'exited', 'exit_code': 0}
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] in ('ok', 'failed'))
    result = client.get('/v1/jobs/' + jid).json()
    assert result['state'] == ('ok' if available else 'failed')
    if available:
        assert json.loads(result['report']['text'])['summary'] == 'chat response'
    else:
        assert result['reason'] == 'chat_result_unavailable'

@pytest.mark.parametrize('system', ['chat'], indirect=True)
def test_tool_free_evidence_must_be_job_bound_and_boolean(system, monkeypatch):
    from tests.b6a.test_dispatch_e2e import LauncherAdapter
    client, _, state = system
    monkeypatch.setattr(LauncherAdapter, 'job_chat_result', lambda self, jid: {
        'job_id': jid, 'tool_free': True,
        'tool_free_evidence': {'job_id': jid, 'runner': 'chat-json', 'profile': 'tool-free-chat-v1',
            'request_tools_absent': 1, 'inspect_verified': True, 'trust_basis': 'host_managed_runner_and_image'},
        'ok': True, 'exit_code': 0, 'json': {'verdict': 'safe', 'concerns': [], 'summary': 'neutral'}}, raising=False)
    response = client.post('/v1/jobs', json={'role': 'second_opinion', 'prompt': 'review', 'sensitivity': 'public',
                                            'verify': 'ax:change-safety-review-v1'})
    jid = response.json()['job_id']
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'running')
    state['jobs'][jid] = {'state': 'exited', 'exit_code': 0}
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'failed')
    result = client.get('/v1/jobs/' + jid).json()
    assert result['reason'] == 'tool_isolation_unverified' and result['tool_free'] is False

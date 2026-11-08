"""Real worker-pool API and dispatch with a local fake model launcher."""
import importlib
import json
from types import SimpleNamespace

import pytest

from plugins.subloop.change.safety_pool import SafetyPool
from tests.b6a.test_dispatch_e2e import system, wait_for  # noqa: F401
from tests.b6a.test_change_profiles_e2e import registered_chat_runner  # noqa: F401
from tests.b14.integration.test_standard_factory_e2e import runtime, server  # noqa: F401


class API:
    def __init__(self, client):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        routes = importlib.import_module('plugins.resource.worker-pool.api').build_routes(client.app.state.service, work_side=True)
        self.client = client
        self.work = TestClient(Starlette(routes=routes))
        self.drop_post = False
        self.drop_get = False
        self.capability_override = None
        self.posts = 0
        self.family_override = None
        self.evidence_override = None

    def post(self, path, body):
        self.posts += 1
        response = self.work.post(path, json=body)
        response.raise_for_status()
        if self.drop_post:
            raise ConnectionError('response lost')
        return response.json()

    def get(self, path):
        if path == '/v1/capabilities' and self.capability_override is not None:
            return self.capability_override
        if self.drop_get:
            raise ConnectionError('lookup unavailable')
        response = self.work.get(path)
        response.raise_for_status()
        result = response.json()
        if self.family_override and result.get('family'):
            result['family'] = self.family_override
        if self.evidence_override and isinstance(result.get('tool_free_evidence'), dict):
            result['tool_free_evidence'].update(self.evidence_override)
        return result


def item():
    return {'provider': 'github', 'issue_repo': 'owner/issues', 'implementation_repo': 'owner/code',
            'kind': 'pr', 'number': 1, 'pr': 1, 'snapshot': {
                'repo': 'owner/code', 'pr': 1, 'head_sha': 'a' * 40,
                'input_revision': 'revision-1', 'body': 'Improve feature', 'diff': '+test'}}


@pytest.fixture
def subject(system, tmp_path):
    client, _, state = system
    api = API(client)
    registry = SimpleNamespace(targets={'owner/issues': SimpleNamespace(implementation_repo='owner/code')})
    path = tmp_path / 'safety.sqlite'
    gate = SafetyPool(api, registry, path)
    yield gate, api, registry, path, state
    gate.close()


def run_stage(gate, api, state, data, stage, *, value=None, expected='ok', malformed=False):
    assert gate.review(data)['state'] == 'parked'
    jobs = api.client.get('/v1/jobs').json()
    rows = jobs if isinstance(jobs, list) else jobs['jobs']
    assert len(rows) == stage + 1
    jid = rows[0]['id'] if len(rows) == 1 else next(j['id'] for j in rows if j['id'] not in state.setdefault('completed_safety', set()))
    wait_for(lambda: api.get('/v1/jobs/' + jid)['state'] == 'running')
    launch = next(call for call in state['calls'] if call['job_id'] == jid)
    assert launch['runner'] == 'chat-json' and launch['safety_profile'] == 'tool-free-chat-v1'
    if value is None:
        value = {'safe': True} if stage >= 2 else {'verdict': 'safe', 'concerns': [], 'summary': 'Improve feature'}
    state['report_log'] = '{invalid json' if malformed else json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': json.dumps(value)})
    state['jobs'][jid] = {'state': 'exited', 'exit_code': 0}
    wait_for(lambda: api.get('/v1/jobs/' + jid)['state'] in ('ok', 'failed'))
    assert api.get('/v1/jobs/' + jid)['state'] == expected
    state.setdefault('completed_safety', set()).add(jid)
    return jid


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_real_pool_two_reviews_screens_and_restart(subject):
    gate, api, registry, path, state = subject
    data = item()
    first = run_stage(gate, api, state, data, 0)
    gate.close()
    gate._db = SafetyPool(api, registry, path)._db
    second = run_stage(gate, api, state, data, 1)
    third = run_stage(gate, api, state, data, 2)
    fourth = run_stage(gate, api, state, data, 3)
    result = gate.review(data)
    assert result['state'] == 'ready' and result['status'] == 'passed'
    assert len(set(result['reviewer_ids'])) == 2
    assert api.get('/v1/jobs/' + second)['prior_job_id'] == first
    assert api.get('/v1/jobs/' + third)['prior_job_id'] == first
    assert api.get('/v1/jobs/' + fourth)['prior_job_id'] == second
    assert gate.review(data)['state'] == 'ready' and api.posts == 4
    changed = item()
    changed['snapshot']['input_revision'] = 'revision-2'
    assert gate.review(changed)['state'] == 'parked'
    assert api.posts == 5


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_unknown_submission_is_not_resent(subject):
    gate, api, registry, path, _ = subject
    api.drop_post = True
    assert gate.review(item())['reason'] == 'safety_submission_unknown'
    gate.close()
    gate._db = SafetyPool(api, registry, path)._db
    api.drop_post = False
    assert gate.review(item())['reason'] == 'safety_submission_unknown'
    assert api.posts == 1


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_independent_family_required(subject):
    gate, api, _, _, state = subject
    data = item()
    run_stage(gate, api, state, data, 0)
    run_stage(gate, api, state, data, 1)
    api.family_override = 'same-family'
    assert gate.review(data)['reason'] == 'safety_not_independent'
    assert api.posts == 2


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_invalid_review_is_never_accepted(subject):
    gate, api, _, _, state = subject
    data = item()
    run_stage(gate, api, state, data, 0, value={'verdict': 'safe'}, expected='failed')
    assert gate.review(data)['reason'] == 'safety_job_failed'
    assert api.posts == 1


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_semantic_screen_rejection_and_scope_change(subject):
    gate, api, _, _, state = subject
    data = item()
    run_stage(gate, api, state, data, 0)
    run_stage(gate, api, state, data, 1)
    run_stage(gate, api, state, data, 2, value={'safe': False})
    assert gate.review(data)['reason'] == 'summary_injection'
    assert api.posts == 3
    changed = item()
    changed['snapshot']['head_sha'] = 'b' * 40
    assert gate.review(changed)['state'] == 'parked' and api.posts == 4
    invalid = item()
    invalid['snapshot']['repo'] = 'other/repo'
    assert gate.review(invalid)['state'] == 'parked' and api.posts == 4


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_malformed_json_is_never_accepted(subject):
    gate, api, _, _, state = subject
    run_stage(gate, api, state, item(), 0, malformed=True, expected='failed')
    assert gate.review(item())['state'] == 'parked' and api.posts == 1


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_lookup_unavailable_is_not_pending(subject):
    gate, api, _, _, _ = subject
    assert gate.review(item())['status'] == 'pending'
    original_get = api.get
    def drop_job(path):
        if path.startswith('/v1/jobs/'):
            raise ConnectionError('lookup unavailable')
        return original_get(path)
    api.get = drop_job
    result = gate.review(item())
    assert result['status'] == 'failed' and result['reason'] == 'safety_lookup_unknown'
    assert api.posts == 1


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_factory_graph_reconciles_actual_pool_jobs(runtime, system, fake_clock):
    from tests.b14.integration.test_standard_factory_e2e import execute

    ex, flow, github_state = runtime
    client, _, model_state = system
    api = API(client)
    flow.ports.safety.pool = api
    ex.apply('run', {'dry_run': False}, 1)
    completed = set()
    for stage in range(4):
        execute(ex)
        rows = flow.ports.collection.store.rows()
        assert len(rows) == 1 and rows[0]['reason'] == 'safety_pending'
        response = client.get('/v1/jobs').json()
        jobs = response if isinstance(response, list) else response['jobs']
        assert len(jobs) == stage + 1
        jid = next(job['id'] for job in jobs if job['id'] not in completed)
        wait_for(lambda: api.get('/v1/jobs/' + jid)['state'] == 'running')
        value = {'safe': True} if stage >= 2 else {'verdict': 'safe', 'concerns': [], 'summary': 'Improve feature'}
        model_state['report_log'] = json.dumps({'type': 'result', 'subtype': 'success',
                                                 'is_error': False, 'result': json.dumps(value)})
        model_state['jobs'][jid] = {'state': 'exited', 'exit_code': 0}
        wait_for(lambda: api.get('/v1/jobs/' + jid)['state'] in ('ok', 'failed'))
        assert api.get('/v1/jobs/' + jid)['state'] == 'ok'
        completed.add(jid)
        fake_clock.advance(61)
    execute(ex)
    rows = flow.ports.collection.store.rows()
    assert rows[0]['reason'] != 'safety_pending'
    records = [row for row in ex.store.items() if not row['item_id'].startswith('run:')]
    saved_rows = [flow.journal.get('pipeline', row['item_id']) for row in records]
    saved = next((entry for entry in saved_rows if entry and 'snapshot' in entry), None)
    assert saved
    evidence = flow.ports.github.evidence_store
    receipt = evidence.journal.get('review_evidence', evidence._scope(saved['item'], saved['snapshot']))
    assert receipt['safety']['status'] == 'passed'
    assert len(receipt['safety']['reviews']) == 2
    assert api.posts == 4
    assert not any(call[0] in ('PATCH', 'PUT') for call in github_state['calls'])


def issue_item():
    data = item()
    data.update(kind='issue', pr=None, project='example')
    data['snapshot'].update(snapshot_type='issue', pr=None, source_issue_repo='owner/issues',
                            source_issue_number=1, project='example', base_ref='main',
                            base_sha='a' * 40, comments_complete=True, diff='',
                            body='{"title":"Improve feature","comments":[]}')
    return data


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_unpublished_issue_review_binds_registered_base_and_revision(subject):
    gate, api, registry, _, state = subject
    registry.targets['owner/issues'] = SimpleNamespace(implementation_repo='owner/code', project='example', base_ref='main')
    data = issue_item()
    for stage in range(4):
        run_stage(gate, api, state, data, stage)
    assert gate.review(data)['status'] == 'passed'
    updated_body = issue_item()
    updated_body['snapshot']['body'] += ' updated'
    updated_body['snapshot']['input_revision'] = 'revision-2'
    assert gate.review(updated_body)['status'] == 'pending'
    updated_base = issue_item()
    updated_base['snapshot']['head_sha'] = updated_base['snapshot']['base_sha'] = 'b' * 40
    assert gate.review(updated_base)['status'] == 'pending'
    assert api.posts == 6


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
@pytest.mark.parametrize('field,value', [
    ('snapshot_type', 'pr'), ('pr', 1), ('source_issue_repo', 'other/repo'),
    ('source_issue_number', 2), ('project', 'other'), ('base_ref', 'other'),
    ('base_sha', 'b' * 40), ('diff', '+diff'), ('comments_complete', False),
])
def test_unpublished_issue_rejects_mismatched_scope(subject, field, value):
    gate, api, registry, _, _ = subject
    registry.targets['owner/issues'] = SimpleNamespace(implementation_repo='owner/code', project='example', base_ref='main')
    data = issue_item()
    data['snapshot'][field] = value
    assert gate.review(data)['status'] == 'failed'
    assert api.posts == 0


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_unpublished_issue_requires_registered_project_and_base(subject):
    gate, api, _, _, _ = subject
    assert gate.review(issue_item())['status'] == 'failed'
    assert api.posts == 0


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_unpublished_issue_rejects_wrong_item_project(subject):
    gate, api, registry, _, _ = subject
    registry.targets['owner/issues'] = SimpleNamespace(implementation_repo='owner/code', project='example', base_ref='main')
    data = issue_item()
    data['project'] = 'wrong'
    assert gate.review(data)['status'] == 'failed' and api.posts == 0


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_unattested_tool_isolation_is_held(subject):
    gate, api, _, _, state = subject
    api.capability_override = {}
    result = gate.review(item())
    assert result['status'] == 'failed'
    assert result['reason'] == 'safety_tool_isolation_unverified'
    assert api.posts == 0


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
@pytest.mark.parametrize('contract', [
    {},
    {'change_safety': {'version': True, 'tool_free_required': True, 'runner': 'chat-json', 'host_profile': 'tool-free-chat-v1'}},
    {'change_safety': {'version': 1, 'tool_free_required': False, 'runner': 'chat-json', 'host_profile': 'tool-free-chat-v1'}},
    {'change_safety': {'version': 1, 'tool_free_required': True, 'runner': 'agent-cli', 'host_profile': 'tool-free-chat-v1'}},
])
def test_incompatible_pool_never_receives_input(subject, contract):
    gate, api, _, _, _ = subject
    api.capability_override = contract
    assert gate.review(item())['reason'] == 'safety_tool_isolation_unverified'
    assert api.posts == 0


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
@pytest.mark.parametrize('bad', [{'job_id': 'different'}, {'profile': 'chat-json'},
                                 {'inspect_verified': 1}, {'request_tools_absent': False}])
def test_wrong_tool_isolation_evidence_is_rejected(subject, bad):
    gate, api, _, _, state = subject
    run_stage(gate, api, state, item(), 0)
    api.evidence_override = bad
    assert gate.review(item())['reason'] == 'safety_tool_isolation_unverified'
    assert api.posts == 1

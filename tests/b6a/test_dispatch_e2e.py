"""Public HTTP job API, resource API and a fake HTTP launcher."""
import importlib
import json
import time
import threading
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient
from axcommon.client import ApiClient
from axcommon.creds import TokenTable, sha256_hex
from core.resources.app import Config as ResourceConfig, build as build_resources
from core.resources.core_auth import CoreAuth


class ResourceAdapter:
    def __init__(self, client, state=None):
        self.client = client
        self.state = state
    def get(self, path, params=None):
        response = self.client.get(path, params=params, headers={'Authorization': 'Bearer control-test'})
        response.raise_for_status()
        return response.json()
    def put(self, path, body):
        response = self.client.put(path, json=body, headers={'Authorization': 'Bearer control-test'})
        response.raise_for_status()
        return response.json()
    def post(self, path, body):
        from axcommon.client import ApiError
        response = self.client.post(path, json=body, headers={'Authorization': 'Bearer pool-test'})
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.json().get('error', {}).get('code', 'http_409'), body=response.json())
        if path == '/v1/slots/reserve' and self.state and self.state.get('reserve_entered'):
            self.state['reserve_entered'].set()
            assert self.state['reserve_continue'].wait(8)
        return response.json()


class LauncherAdapter:
    def __init__(self, client):
        self.client = client
    def job_start(self, req):
        raise NotImplementedError
    def job_status(self, job_id):
        r = self.client.get('/status/' + job_id); r.raise_for_status(); return r.json()
    def job_stop(self, job_id):
        r = self.client.post('/stop/' + job_id, json={}); r.raise_for_status(); return r.json()
    def job_log(self, job_id):
        r = self.client.get('/log/' + job_id); r.raise_for_status(); return r.json()


class Control:
    def get(self, path):
        return {'mode': 'run', 'revision': 1, 'knobs': {}}


def wait_for(fn):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(.03)
    pytest.fail('condition timed out')


@pytest.fixture
def system(tmp_path, monkeypatch, request):
    from axcommon.creds import sha256_hex
    table = tmp_path / "pool-core-tokens"
    table.write_text("intake " + sha256_hex("test-intake") + "\n")
    monkeypatch.setenv("AX_CORE_TOKENS", str(table))
    state = {'jobs': {}, 'calls': [], 'start_failure': False, 'unknown': False, 'stop_failure': False, 'verify_rc': 0, 'report_log': 'finished'}
    launcher_module = importlib.import_module('plugins.resource.worker-pool.launcher_client')
    def start(self, req):
        try:
            r = self.client.post('/start', json=req); r.raise_for_status(); return r.json()
        except Exception as exc:
            raise launcher_module.LauncherError(2, 'rejected') from exc
    LauncherAdapter.job_start = start
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse
    async def start_handler(request):
        body = await request.json()
        state['calls'].append(body)
        if state.get('start_entered'):
            state['start_entered'].set()
            assert state['start_continue'].wait(8)
        if state['start_failure']:
            return JSONResponse({'error': 'rejected'}, status_code=400)
        state['jobs'][body['job_id']] = ({'state': 'exited', 'exit_code': state['verify_rc']}
            if body['runner'].startswith('verify-') else {'state': 'running'})
        return JSONResponse({'job_id': body['job_id']})
    async def stop_handler(request):
        jid = request.path_params['jid']
        state.setdefault('stops', []).append(jid)
        if state['stop_failure']:
            return JSONResponse({}, status_code=503)
        state['jobs'][jid] = {'state': 'exited', 'exit_code': 137}
        return JSONResponse(state['jobs'][jid])
    async def status_handler(request):
        if state['unknown']:
            return JSONResponse({}, status_code=503)
        return JSONResponse(state['jobs'].get(request.path_params['jid'], {'state': 'unknown'}))
    async def log_handler(request):
        return JSONResponse({'log': 'verify output' if request.path_params['jid'].startswith('verify-') else state['report_log']})
    fake = TestClient(Starlette(routes=[Route('/start', start_handler, methods=['POST']),
        Route('/stop/{jid}', stop_handler, methods=['POST']), Route('/status/{jid}', status_handler),
        Route('/log/{jid}', log_handler)]))
    models = tmp_path / 'models.yaml'
    model_rows = [{'id': 'example/model', 'kind': 'model', 'family': 'example',
                   'tier': 'standard', 'billing': 'flat', 'level': 'L1', 'vision': False, 'runner': 'agent-cli', 'status': 'active'}]
    if getattr(request, 'param', None) == 'chat':
        model_rows[0]['runner'] = 'chat-json'
    if getattr(request, 'param', None) == 'manager':
        model_rows[0]['level'] = 'L3'
        model_rows[0]['family'] = 'glm'
    if getattr(request, 'param', None) == 'two':
        model_rows.append({**model_rows[0], 'id': 'example/model-z'})
    if getattr(request, 'param', None) == 'two_models':
        model_rows.append({**model_rows[0], 'id': 'other/model', 'family': 'other'})
    if getattr(request, 'param', None) == 'families':
        model_rows.extend([{**model_rows[0], 'id': 'example/model-z'}, {**model_rows[0], 'id': 'other/model', 'family': 'other'}])
    if getattr(request, 'param', None) == 'registered':
        models = Path(__file__).resolve().parents[2] / 'config/models.yaml'
        model_rows = yaml.safe_load(models.read_text())['models']
    else:
        models.write_text(yaml.safe_dump({'models': model_rows}))
    auth = CoreAuth(TokenTable({'worker-pool': sha256_hex('pool-test'), 'control': sha256_hex('control-test')}))
    resource_app, _, _ = build_resources(ResourceConfig(db=str(tmp_path / 'r.sqlite'), models_file=str(models), worker_pool_db=str(tmp_path / 'p.sqlite')), auth=auth, run_heartbeat_loop=False)
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    with TestClient(resource_app) as resource:
        caps = {row['id']: 1 for row in model_rows}
        families = {row['family']: 1 for row in model_rows}
        assert resource.put('/v1/slots/effective-limits', headers={'Authorization': 'Bearer control-test'}, json={'revision': 1, 'expires_at': time.time() + 3600, 'models': caps, 'families': families}).status_code == 200
        def emit(kind, subject, data, **kwargs):
            state.setdefault('events', []).append((kind, subject, data))
            if state.get('emit_failure'):
                raise RuntimeError('sink unavailable')
        core, _, service = pool.build(pool.Config(db=str(tmp_path / 'p.sqlite')), resources=ResourceAdapter(resource, state), control=Control(),
                                launcher=LauncherAdapter(fake), dispatch_interval_s=.05,
                                run_heartbeat_loop=False, emit=emit)
        class LessonTimeline:
            def get_events(self, *, after, kind, limit, wait):
                target_events = [e for e in state.get('lessons', []) if e['seq'] > after and e['kind'] == kind]
                state['lesson_reads'] = state.get('lesson_reads', 0) + 1
                if state.get('lesson_failure'):
                    raise OSError('timeline unavailable')
                rows = target_events[:limit]
                return {'events': rows, 'next_after': rows[-1]['seq'] if rows else after}
        service.lesson_timeline = LessonTimeline()
        with TestClient(core, headers={"Authorization": "Bearer test-intake"}) as client:
            yield client, resource, state
    fake.close()


def submit(client, prompt='do work'):
    r = client.post('/v1/jobs', json={'role': 'summary', 'prompt': prompt, 'sensitivity': 'public'})
    assert r.status_code == 202
    return r.json()['job_id']


def test_capacity_completion_and_cancel(system):
    client, resources, state = system
    a = submit(client)
    wait_for(lambda: client.get('/v1/jobs/' + a).json()['state'] == 'running')
    b = submit(client)
    time.sleep(.3)
    assert client.get('/v1/jobs/' + b).json()['state'] == 'queued'
    assert len(state['calls']) == 1 and state['calls'][0]['prompt'] == 'do work'
    state['jobs'][a] = {'state': 'exited', 'exit_code': 0}
    wait_for(lambda: client.get('/v1/jobs/' + b).json()['state'] == 'running')
    assert client.get('/v1/jobs/' + a).json()['report']['text'] == 'finished'
    state['stop_failure'] = True
    client.post('/v1/jobs/' + b + '/cancel')
    time.sleep(.2)
    assert resources.get('/v1/slots').json()['models']['example/model']['used'] == 1
    state['stop_failure'] = False
    wait_for(lambda: client.get('/v1/jobs/' + b).json()['state'] == 'cancelled')
    wait_for(lambda: resources.get('/v1/slots').json()['models']['example/model']['used'] == 0)


def test_failure_unknown_and_empty_prompt(system):
    client, resources, state = system
    state['start_failure'] = True
    a = submit(client)
    wait_for(lambda: client.get('/v1/jobs/' + a).json()['state'] == 'failed')
    wait_for(lambda: resources.get('/v1/slots').json()['models']['example/model']['used'] == 0)
    state['start_failure'] = False
    state['unknown'] = True
    b = submit(client)
    wait_for(lambda: client.get('/v1/jobs/' + b).json()['state'] == 'running')
    time.sleep(.2)
    assert resources.get('/v1/slots').json()['models']['example/model']['used'] == 1
    count = len(state['calls'])
    c = submit(client, '')
    wait_for(lambda: client.get('/v1/jobs/' + c).json()['state'] == 'rejected')
    assert len(state['calls']) == count


@pytest.mark.parametrize('report,verify,rc,expected,expected_report,human', [
    ('STATUS: ready', 'true', 0, 'ok', 'ready', ''),
    ('[cli:warning] diagnostic\n' + json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'created file\nSTATUS: ready'}), 'true', 0, 'ok', 'ready', ''),
    ('[cli:warning] diagnostic\n' + json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'created file'}), 'true', 0, 'failed', 'failed', ''),
    ('[cli:warning] diagnostic\n' + json.dumps({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'result': 'STATUS: ready'}), 'true', 0, 'failed', 'failed', ''),
    (json.dumps({'type': 'assistant', 'is_error': False, 'result': 'STATUS: ready'}), 'true', 0, 'failed', 'failed', ''),
    (json.dumps({'type': 'result', 'is_error': False, 'result': 'STATUS: ready'}) + '\n[cli:warning] incomplete', 'true', 0, 'failed', 'failed', ''),
    ('[cli:warning] diagnostic\nSTATUS: ready', 'true', 0, 'failed', 'failed', ''),
    (json.dumps({'result': 'STATUS: ready'}), 'true', 0, 'failed', 'failed', ''),
    ('STATUS: ready', 'false', 1, 'failed', 'ready', ''),
    ('STATUS: blocked\nHUMAN_ACTION: provide access', 'true', 0, 'failed', 'blocked', 'provide access'),
    ('STATUS: failed', 'true', 0, 'failed', 'failed', ''),
    ('STATUS: dropped', 'true', 0, 'failed', 'dropped', ''),
    ('no status', 'true', 0, 'failed', 'failed', ''),
    ('STATUS: ready', None, 0, 'failed', 'ready', ''),
])
def test_verification_result_and_report(system, report, verify, rc, expected, expected_report, human):
    client, resources, state = system
    state['report_log'], state['verify_rc'] = report, rc
    body = {'role': 'summary', 'prompt': 'work', 'sensitivity': 'public'}
    if verify is not None:
        body['verify'] = {'cmd': verify}
    response = client.post('/v1/jobs', json=body)
    assert response.status_code == 202
    jid = response.json()['job_id']
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'running')
    state['jobs'][jid] = {'state': 'exited', 'exit_code': 0, 'wd': jid}
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] in ('ok', 'failed'))
    result = client.get('/v1/jobs/' + jid).json()
    assert result['state'] == expected
    assert result['report']['status'] == expected_report
    assert result['report']['human_action'] == human
    attempts = client.app.state.service.store.attempts(jid)
    assert len(attempts) == 1
    assert attempts[0]['state'] == expected
    assert attempts[0]['model'] == 'example/model'
    verification_expected = verify is not None and expected_report != 'failed' or (verify is not None and report == 'STATUS: failed')
    assert attempts[0]['verify_rc'] == (rc if verification_expected else None)
    if verification_expected:
        assert result['verify']['rc'] == rc
        requests = [call for call in state['calls'] if call['runner'].startswith('verify-')]
        assert len(requests) == 1 and requests[0]['verify'] == verify and requests[0]['workdir_id'] == jid
    else:
        assert result['verify']['rc'] is None
    wait_for(lambda: resources.get('/v1/slots').json()['models']['example/model']['used'] == 0)


def test_review_timeout_reconciles_only_after_grace(tmp_path):
    from urllib.parse import urlsplit
    from starlette.applications import Starlette
    from core.resources.api import build_routes
    from plugins.subloop.release.gate.adapters import GatewayReviews

    models = tmp_path / 'review-models.yaml'
    models.write_text(yaml.safe_dump({'models': [{'id': 'example/model', 'kind': 'model', 'family': 'example',
        'tier': 'standard', 'level': 'L3', 'runner': 'agent-cli', 'status': 'active'}]}))
    auth = CoreAuth(TokenTable({'worker-pool': sha256_hex('pool-test'), 'release-review': sha256_hex('review-test'), 'control': sha256_hex('control-test')}))
    resource_app, _, service = build_resources(ResourceConfig(db=str(tmp_path / 'resource.sqlite'),
                                                models_file=str(models)), auth=auth, run_heartbeat_loop=False)
    clock = [1000.0]
    service.review_clock = lambda: clock[0]
    service.review_grace_s = 60
    review_app = Starlette(routes=build_routes(service, work_side=False, review_side=True))
    sent = []
    class Gateway:
        def post(self, path, body):
            sent.append(body)
            raise TimeoutError('gateway offline')
    pool_module = importlib.import_module('plugins.resource.worker-pool.app')
    with TestClient(resource_app) as core, TestClient(review_app) as review:
        assert core.put('/v1/slots/effective-limits', headers={'Authorization': 'Bearer control-test'}, json={'revision': 1, 'expires_at': time.time() + 3600, 'models': {'example/model': 1}, 'families': {'example': 1}}).status_code == 200
        payload = {'reservation_id': 'publish-review-probe', 'model': 'example/model'}
        assert review.get('/v1/resources').status_code == 404
        assert review.get('/v1/slots').status_code == 404
        assert review.post('/v1/slots/reconcile-reviews', json={}).status_code == 404
        assert review.post('/v1/slots/reserve', json=payload).status_code == 401
        assert review.post('/v1/slots/reserve', headers={'Authorization': 'Bearer pool-test'}, json=payload).status_code == 403
        assert review.post('/v1/slots/reserve', headers={'Authorization': 'Bearer review-test'},
                           json={'reservation_id': 'other', 'model': 'example/model'}).status_code == 403
        def transport(method, url, headers, body, timeout):
            response = review.request(method, urlsplit(url).path, headers=headers, content=body)
            return response.status_code, dict(response.headers), response.content
        slots = ApiClient('http://review', 'review-test', transport=transport)
        reviews = GatewayReviews(None, Gateway(), tmp_path / 'unused.yaml', slots=slots)
        with pytest.raises(TimeoutError):
            reviews.execute({'id': 'example/model', 'family': 'example'},
                            {'tools_enabled': False, 'mcp_enabled': False, 'memory_enabled': False})
        ident = sent[0]['user']
        assert ident.startswith('publish-review-')
        headers = {'Authorization': 'Bearer pool-test'}
        def outcome():
            return core.get('/v1/slots/review-outcomes/' + ident, headers=headers).json()
        assert outcome()['state'] == 'unknown'
        assert core.get('/v1/slots').json()['models']['example/model']['used'] == 1
        assert review.post('/v1/slots/release', headers={'Authorization': 'Bearer review-test'},
                           json={'reservation_id': ident}).json()['released'] is False
        pool, _, _ = pool_module.build(pool_module.Config(db=str(tmp_path / 'pool.sqlite')),
            resources=ResourceAdapter(core), control=Control(), launcher=object(),
            dispatch_interval_s=.05, run_heartbeat_loop=False)
        with TestClient(pool):
            clock[0] = 1059.0
            time.sleep(.2)
            assert outcome()['state'] == 'unknown'
            assert core.get('/v1/slots').json()['models']['example/model']['used'] == 1
            clock[0] = 1060.0
            wait_for(lambda: outcome()['state'] == 'released_after_grace')
            assert core.get('/v1/slots').json()['models']['example/model']['used'] == 0
            assert outcome()['released_at'] == 1060.0


def test_review_network_binds_only_private_interface(repo_root):
    from core.resources.netbind import resolve_review_binds
    compose = yaml.safe_load((repo_root / 'compose.yaml').read_text())
    assert compose['networks']['ax-publish-review']['internal'] is True
    users = []
    for path in sorted((repo_root / 'compose.d').glob('*.yaml')):
        doc = yaml.safe_load(path.read_text()) or {}
        for name, service in (doc.get('services') or {}).items():
            if 'ax-publish-review' in (service.get('networks') or {}):
                users.append(name)
                assert not service.get('ports')
    assert sorted(users) == ['resources', 'sl-release']
    assert resolve_review_binds({}, ['127.0.0.1', '192.0.2.11', '198.51.100.11', '192.0.2.139']) == ['192.0.2.139']


def test_invalid_limit_update_does_not_stop_unrelated_job(system):
    client, resources, state = system
    invalid = {'revision': 2, 'expires_at': time.time() + 3600,
               'models': {'example/model': -1}, 'families': {'example': 1}}
    response = resources.put('/v1/slots/effective-limits',
                             headers={'Authorization': 'Bearer control-test'}, json=invalid)
    assert response.status_code == 400 and response.json()['error']
    job = submit(client, 'unrelated work')
    wait_for(lambda: client.get('/v1/jobs/' + job).json()['state'] == 'running')
    assert any(call['job_id'] == job for call in state['calls'])


def test_selected_lessons_reach_only_matching_project(system):
    client, _, state = system
    state['lessons'] = [
        {'seq': 1, 'kind': 'lesson.selected', 'data': {'target': 'project-a', 'lesson_id': 'la', 'body': 'LESSON_A'}},
        {'seq': 2, 'kind': 'lesson.selected', 'data': {'target': 'project-b', 'lesson_id': 'lb', 'body': 'LESSON_B'}},
        {'seq': 3, 'kind': 'lesson.candidate', 'data': {'target': 'project-a', 'body': 'CANDIDATE'}},
    ]
    response = client.post('/v1/jobs', json={'role': 'summary', 'prompt': 'work', 'sensitivity': 'public', 'project': 'project-a'})
    assert response.status_code == 202
    jid = response.json()['job_id']
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'running')
    text = next(call['prompt'] for call in state['calls'] if call['job_id'] == jid)
    assert 'LESSON_A' in text and 'LESSON_B' not in text and 'CANDIDATE' not in text


def test_unavailable_lessons_hold_only_target_job_and_retry(system):
    client, _, state = system
    state['lesson_failure'] = True
    response = client.post('/v1/jobs', json={'role': 'summary', 'prompt': 'target work', 'sensitivity': 'public', 'project': 'project-a'})
    assert response.status_code == 202
    held = response.json()['job_id']
    wait_for(lambda: state.get('lesson_reads', 0) > 0)
    assert client.get('/v1/jobs/' + held).json()['state'] == 'queued'
    other = submit(client, 'independent work')
    wait_for(lambda: client.get('/v1/jobs/' + other).json()['state'] == 'running')
    assert all(call['job_id'] != held for call in state['calls'])
    state['jobs'][other] = {'state': 'exited', 'exit_code': 0}
    state['lesson_failure'] = False
    wait_for(lambda: client.get('/v1/jobs/' + held).json()['state'] == 'running')


def test_lesson_pagination_includes_later_selected_event(system):
    client, _, state = system
    state['lessons'] = [
        {'seq': n, 'kind': 'lesson.selected', 'data': {'target': 'other', 'lesson_id': str(n), 'body': 'OTHER'}}
        for n in range(1, 1001)
    ]
    state['lessons'].append({'seq': 1001, 'kind': 'lesson.selected', 'data': {
        'target': 'project-a', 'lesson_id': 'last', 'body': 'LATER_LESSON'}})
    response = client.post('/v1/jobs', json={'role': 'summary', 'prompt': 'work', 'sensitivity': 'public', 'project': 'project-a'})
    assert response.status_code == 202
    jid = response.json()['job_id']
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'running')
    text = next(call['prompt'] for call in state['calls'] if call['job_id'] == jid)
    assert 'LATER_LESSON' in text and 'OTHER' not in text


def test_cancel_after_reservation_before_start(system):
    client, resources, state = system
    state['reserve_entered'], state['reserve_continue'] = threading.Event(), threading.Event()
    jid = submit(client)
    try:
        assert state['reserve_entered'].wait(8)
        response = client.post('/v1/jobs/' + jid + '/cancel')
        assert response.status_code == 200
        assert response.json()['job']['state'] == 'cancelled'
        assert client.get('/v1/jobs/' + jid).json()['state'] == 'cancelled'
        assert not state['calls']
    finally:
        state['reserve_continue'].set()
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'cancelled')
    assert not state['calls']
    wait_for(lambda: resources.get('/v1/slots').json()['models']['example/model']['used'] == 0)
    state['reserve_entered'] = None
    other = submit(client, 'other work')
    wait_for(lambda: client.get('/v1/jobs/' + other).json()['state'] == 'running')
    assert [call['job_id'] for call in state['calls']] == [other]


def test_cancel_during_start_waits_then_stops_only_target(system):
    client, resources, state = system
    state['start_entered'], state['start_continue'] = threading.Event(), threading.Event()
    jid = submit(client)
    done = threading.Event()
    result = []
    def cancel():
        result.append(client.post('/v1/jobs/' + jid + '/cancel'))
        done.set()
    try:
        assert state['start_entered'].wait(8)
        other = submit(client, 'other work')
        thread = threading.Thread(target=cancel)
        thread.start()
        assert not done.wait(.2)
    finally:
        state['start_continue'].set()
    assert done.wait(8)
    thread.join()
    assert result[0].status_code == 200
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'cancelled')
    job = client.get('/v1/jobs/' + jid).json()
    assert job['cancel_requested'] is True
    assert job['report']['text'] == 'finished'
    assert state['jobs'][jid]['exit_code'] == 137
    state['start_entered'] = None
    wait_for(lambda: client.get('/v1/jobs/' + other).json()['state'] == 'running')
    assert state['jobs'][other]['state'] == 'running'
    assert state['stops'] == [jid]

@pytest.mark.parametrize('system', ['two'], indirect=True)
def test_control_limits_dispatch_restart_and_failures(system, tmp_path, monkeypatch):
    from pathlib import Path
    from axcommon.client import Unavailable
    from core.control.service import ControlService
    from core.control.store import Store
    from core.control.app import create_core_app

    pool, resources, state = system
    repo = Path(__file__).resolve().parents[2]
    registry = tmp_path / 'registry.yaml'
    registry.write_text(yaml.safe_dump({'schema_version': 1, 'plugins': [
        {'id': 'worker-pool', 'kind': 'resource', 'path': 'plugins/resource/worker-pool/manifest.yaml'}]}))
    token_table = tmp_path / 'core-tokens.sha256'
    token_table.write_text('intake ' + sha256_hex('intake-test') + '\n'
                           + 'hq ' + sha256_hex('test-intake') + '\n')
    monkeypatch.setenv('AX_CORE_TOKENS', str(token_table))
    secrets = tmp_path / 'secrets'
    secrets.mkdir()
    (secrets / 'control-token-manager').write_text('manager-test')
    monkeypatch.setenv('AX_SECRETS_PATH', str(secrets))
    headers = {'Authorization': 'Bearer intake-test'}
    worker_headers = {'Authorization': 'Bearer pool-test'}
    adapter = ResourceAdapter(resources)
    db = tmp_path / 'control.sqlite'

    def new_control():
        service = ControlService(Store(db), None, registry_root=repo, registry_file=registry, resources_client=adapter)
        return service, TestClient(create_core_app(service, ext_dir=str(tmp_path / 'no-ext')))

    def set_cap(control, value):
        return control.post('/v1/commands', headers=headers, json={
            'target': 'worker-pool', 'op': 'set_knob', 'args': {'name': 'model_cap', 'value': value},
            'issued_by': 'user', 'reason': 'test cap'})

    service, control_client = new_control()
    with control_client as control:
        initial_rev = control.get('/healthz').json()['effective_limits']['revision']
        assert initial_rev > 1
        a = submit(pool)
        wait_for(lambda: pool.get('/v1/jobs/' + a).json()['state'] == 'running')
        b = submit(pool)
        wait_for(lambda: pool.get('/v1/jobs/' + b).json()['state'] == 'running')
        assert [state['calls'][0]['model'], state['calls'][1]['model']] == ['example/model', 'example/model-z']
        c = submit(pool)
        wait_for(lambda: pool.get('/v1/jobs/' + c).json().get('reason') == 'capacity_exhausted')
        assert pool.get('/v1/jobs/' + c).json()['state'] == 'queued'
        assert pool.post('/v1/jobs/' + c + '/cancel').json()['job']['state'] == 'cancelled'
        assert set_cap(control, {'example/model': 2}).status_code == 201
        d = submit(pool)
        wait_for(lambda: pool.get('/v1/jobs/' + d).json()['state'] == 'running')
        assert state['calls'][-1]['model'] == 'example/model'
        assert pool.get('/v1/jobs/' + c).json()['state'] == 'cancelled'

        # Hold the acknowledged freeze before the command can be committed.
        entered, proceed = threading.Event(), threading.Event()
        original_put = adapter.put
        def pause_freeze(path, body):
            result = original_put(path, body)
            if not body['models']:
                entered.set()
                assert proceed.wait(8)
            return result
        adapter.put = pause_freeze
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(set_cap(control, {'example/model': 0})))
        thread.start()
        try:
            assert entered.wait(8)
            denial = resources.post('/v1/slots/reserve', headers=worker_headers,
                                    json={'reservation_id': 'during-lowering', 'model': 'example/model'})
            assert denial.status_code == 409 and denial.json()['reason'] == 'limits_missing'
            assert all(pool.get('/v1/jobs/' + jid).json()['state'] == 'running' for jid in (a, b, d))
        finally:
            proceed.set()
            thread.join(8)
            adapter.put = original_put
        assert not thread.is_alive() and outcome[0].status_code == 201
        assert resources.get('/v1/slots').json()['models']['example/model']['cap'] == 0

        # Freeze failure rejects the command and records the failure.
        def down(path, body):
            raise Unavailable('offline')
        adapter.put = down
        failed = set_cap(control, {'example/model': 1})
        assert failed.status_code == 503 and failed.json()['error']['code'] == 'limits_unavailable'
        assert control.get('/healthz').json()['effective_limits']['status'] == 'failed'
        assert resources.get('/v1/slots').json()['models']['example/model']['cap'] == 0
        adapter.put = original_put
        # The freeze can succeed while the following publication fails.
        puts = []
        def fail_after_freeze(path, body):
            puts.append(body)
            if body['models']:
                raise Unavailable('offline after freeze')
            return original_put(path, body)
        adapter.put = fail_after_freeze
        assert set_cap(control, {'example/model': 1}).status_code == 201
        assert len(puts) == 2 and control.get('/healthz').json()['effective_limits']['status'] == 'failed'
        denied = resources.post('/v1/slots/reserve', headers=worker_headers,
                                json={'reservation_id': 'during-outage', 'model': 'example/model'})
        assert denied.status_code == 409 and denied.json()['reason'] == 'limits_missing'
        during_outage = submit(pool, 'outage')
        wait_for(lambda: pool.get('/v1/jobs/' + during_outage).json().get('reason') == 'limits_missing')
        assert not any(call['job_id'] == during_outage for call in state['calls'])
        assert pool.post('/v1/jobs/' + during_outage + '/cancel').json()['job']['cancel_requested'] is True
        wait_for(lambda: pool.get('/v1/jobs/' + during_outage).json()['state'] == 'cancelled')
        adapter.put = original_put
        before_restart = control.get('/healthz').json()['effective_limits']['revision']
    service.store.close()

    adapter.put = down
    restarted, restarted_client = new_control()
    with restarted_client as control:
        status = control.get('/healthz').json()['effective_limits']
        assert status['status'] == 'failed' and control.get('/v1/plugins/worker-pool/effective').status_code == 200
        adapter.put = original_put
        assert control.post('/v1/sweep', headers={'Authorization': 'Bearer manager-test'}, json={}).status_code == 200
        status = control.get('/healthz').json()['effective_limits']
        assert status['status'] == 'ok' and status['revision'] > before_restart
        assert resources.get('/v1/slots').json()['models']['example/model']['cap'] == 1
        assert set_cap(control, {'example/model': 3}).status_code == 201
        e = submit(pool)
        wait_for(lambda: pool.get('/v1/jobs/' + e).json()['state'] == 'running')
        assert pool.get('/v1/jobs/' + c).json()['state'] == 'cancelled'
        assert len([call for call in state['calls'] if call['job_id'] == c]) == 0
        revision = control.get('/healthz').json()['effective_limits']['revision']
        expired = {'revision': revision + 1, 'expires_at': time.time() - 1,
                   'models': {'example/model': 3, 'example/model-z': 1}, 'families': {'example': 4}}
        assert resources.put('/v1/slots/effective-limits', headers={'Authorization': 'Bearer control-test'}, json=expired).status_code == 200
        expired_job = submit(pool, 'expired')
        wait_for(lambda: pool.get('/v1/jobs/' + expired_job).json().get('reason') == 'limits_expired')
        assert not any(call['job_id'] == expired_job for call in state['calls'])
    restarted.store.close()

@pytest.mark.parametrize('initial_state,cancel_requested', [
    ('reserving', True), ('cancelled', True), ('reserving', False),
])
def test_reservation_recovery_after_restart(system, tmp_path, initial_state, cancel_requested):
    _, resources, state = system
    jobs = importlib.import_module('plugins.resource.worker-pool.jobs')
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    path = str(tmp_path / 'restart-pool.sqlite')
    store = jobs.JobsStore(path)
    job = store.create({'role': 'summary', 'prompt': 'recover', 'state': initial_state})
    jid = job['id']
    with store._lock:
        store._conn.execute('UPDATE jobs SET model=?, cancel_requested=? WHERE id=?',
                            ('example/model', int(cancel_requested), jid))
    adapter = ResourceAdapter(resources)
    adapter.post('/v1/slots/reserve', {'reservation_id': jid, 'model': 'example/model'})
    store.close()

    class Launcher:
        def job_start(self, req):
            state['calls'].append(req)
        def job_status(self, jid):
            return {'state': 'running'}

    core, _, service = pool.build(pool.Config(db=path), resources=adapter, control=Control(),
                                   launcher=Launcher(), run_heartbeat_loop=False)
    service.apply_effective({'mode': 'run', 'revision': 1, 'knobs': {}})
    service.dispatcher.tick()
    with TestClient(core, headers={'Authorization': 'Bearer test-intake'}) as client:
        result = client.get('/v1/jobs/' + jid).json()
        if cancel_requested:
            assert result['state'] == 'cancelled'
            assert not state['calls']
            assert resources.get('/v1/slots').json()['models']['example/model']['used'] == 0
        else:
            assert result['state'] == 'running'
            assert [call['job_id'] for call in state['calls']] == [jid]
    service.store.close()


@pytest.mark.parametrize('system', ['registered'], indirect=True)
def test_registered_models_fallback_via_public_api(system):
    client, resources, state = system
    expected = {
        'auto/glm-5.3': ('glm', 'standard'),
        'auto/glm-5.3-flash': ('glm-flash', 'flash'),
        'group/opencode-deepseek-v4.1-flash': ('deepseek-flash', 'flash'),
        'our-free-model/buddy/hy3': ('hy3', 'free'),
        'group/beam-free': ('beam', 'free'),
        'our-free-model/buddy/step-5-preview': ('step', 'free'),
    }
    result = resources.get('/v1/resources').json()
    rows = result['resources']
    assert {row['id']: (row['family'], row['tier']) for row in rows} == expected
    reserve = 'our-free-model/buddy/hy3'
    for i, model in enumerate(expected):
        if model != reserve:
            response = resources.post('/v1/command-exclusions', headers={'Authorization': 'Bearer control-test'}, json={
                'command_id': f'exclude-{i}', 'resource_id': model, 'active': True,
                'reason': 'test', 'until': '2099-01-01T00:00:00Z'})
            assert response.status_code == 200
    job = submit(client)
    wait_for(lambda: client.get('/v1/jobs/' + job).json()['state'] == 'running')
    assert state['calls'][0]['model'] == reserve
    assert all('inferera' not in row['id'] and 'dots3' not in row['id'] and 'tokenharbor' not in row['id'] for row in rows)


@pytest.mark.parametrize('system', ['manager'], indirect=True)
def test_draining_continues_existing_items_and_managers(system):
    client, _, state = system
    service = client.app.state.service
    def post(role='summary', item_id=None):
        body = {'role': role, 'prompt': 'work', 'sensitivity': 'public'}
        if item_id is not None:
            body['item_id'] = item_id
        result = client.post('/v1/jobs', json=body)
        assert result.status_code == 202
        return result.json()['job_id']
    prior = post(item_id='existing')
    wait_for(lambda: any(c['job_id'] == prior for c in state['calls']))
    service.apply_effective({'mode': 'draining', 'revision': 2, 'knobs': {}})
    continued = post(item_id='existing')
    fresh = post(item_id='fresh')
    anonymous = post()
    manager = post(role='manager')
    state['report_log'] = 'STATUS: ready'
    state['jobs'][prior] = {'state': 'exited', 'exit_code': 0}
    wait_for(lambda: any(c['job_id'] in (continued, manager) for c in state['calls']))
    remaining = {continued, manager}
    while remaining:
        wait_for(lambda: any(c['job_id'] in remaining for c in state['calls']))
        job = next(c['job_id'] for c in state['calls'] if c['job_id'] in remaining)
        state['jobs'][job] = {'state': 'exited', 'exit_code': 0}
        remaining.remove(job)
    time.sleep(.2)
    assert not any(c['job_id'] in (fresh, anonymous) for c in state['calls'])


def test_event_delivery_failure_does_not_stop_job(system):
    client, _, state = system
    state['emit_failure'] = True
    state['report_log'] = 'STATUS: ready'
    response = client.post('/v1/jobs', json={'role': 'summary', 'prompt': 'work', 'sensitivity': 'public', 'verify': 'true'})
    assert response.status_code == 202
    job = response.json()['job_id']
    wait_for(lambda: job in state['jobs'])
    state['jobs'][job] = {'state': 'exited', 'exit_code': 0}
    wait_for(lambda: client.get('/v1/jobs/' + job).json()['state'] == 'ok')
    kinds = {event[0] for event in state['events'] if event[1] == job}
    assert {'job.state', 'worker.selection', 'worker.attempt'} <= kinds


def test_outcome_public_api_deduplicates_by_attempt_and_tracks_level(system):
    client, resources, state = system
    headers = {'Authorization': 'Bearer pool-test'}
    url = '/v1/resources/example/model/outcome'
    def result(trial, outcome='pass', rc=0, counts='no', **extra):
        body = {'job_id': 'work-' + str(trial), 'trial_id': str(trial), 'outcome': outcome,
                'verify_rc': rc, 'counts_as_failure': counts, **extra}
        response = resources.post(url, headers=headers, json=body)
        assert response.status_code == 200, response.text
        return response.json()['resource']
    assert resources.get('/v1/resources/example/model').status_code == 200
    legacy = {'job_id': 'legacy-without-verification'}
    assert resources.post(url, headers=headers, json=legacy).json()['resource']['stats']['last']['counts_as_failure'] == 'hold'
    assert resources.post(url, headers=headers, json=legacy).json()['resource']['stats']['trials'] == 1
    assert result(1)['level'] == 'L1'
    assert result(1)['stats']['trials'] == 2
    assert result(2)['level'] == 'L1'
    for error in ('quota', 'rate_limit', 'connect', 'system.expected'):
        assert result('hold-' + error, 'fail', 1, 'yes', error_class=error)['stats']['last']['counts_as_failure'] == 'hold'
    assert result(3)['level'] == 'L2'
    assert result(3)['level'] == 'L2'
    assert result(4)['level'] == 'L2'
    assert result(5)['level'] == 'L2'
    assert result(6)['level'] == 'L3'
    assert result('net', 'fail', 1, 'yes', error_class='network')['level'] == 'L3'
    assert result('f1', 'fail', 1, 'yes', error_class='capability')['level'] == 'L3'
    assert result('f2', 'fail', 1, 'yes', error_class='capability')['level'] == 'L2'
    assert result('f2', 'fail', 1, 'yes', error_class='capability')['level'] == 'L2'
    assert result('f3', 'fail', 1, 'yes', error_class='capability')['level'] == 'L2'
    assert resources.post('/v1/resources/example/model/prefix_cache', headers=headers, json={'value': 'unknown'}).status_code == 200


def test_verified_job_outcome_retries_without_changing_completion(system):
    client, resources, state = system
    svc = client.app.state.service
    original = svc.resources.post
    state['outcome_down'] = True
    def flaky(path, body):
        if path.endswith('/outcome') and state['outcome_down']:
            raise OSError('unavailable')
        return original(path, body)
    svc.resources.post = flaky
    state['report_log'] = 'STATUS: ready'
    response = client.post('/v1/jobs', json={'role': 'summary', 'prompt': 'work',
                                           'sensitivity': 'public', 'verify': {'cmd': 'true'}})
    assert response.status_code == 202
    jid = response.json()['job_id']
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'running')
    state['jobs'][jid] = {'state': 'exited', 'exit_code': 0, 'wd': jid}
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'ok')
    assert resources.get('/v1/resources/example/model').json()['stats']['trials'] == 0
    other = submit(client, 'another job')
    wait_for(lambda: client.get('/v1/jobs/' + other).json()['state'] == 'running')
    state['outcome_down'] = False
    wait_for(lambda: resources.get('/v1/resources/example/model').json()['stats']['trials'] == 1)
    assert client.get('/v1/jobs/' + jid).json()['state'] == 'ok'
    wait_for(lambda: svc.store.attempts(jid)[0]['outcome_sent'] == 1)
    svc.store._conn.execute('UPDATE attempts SET outcome_sent=0 WHERE job_id=?', (jid,))
    wait_for(lambda: svc.store.attempts(jid)[0]['outcome_sent'] == 1)
    assert resources.get('/v1/resources/example/model').json()['stats']['trials'] == 1


def test_verified_jobs_promote_and_unverified_failure_is_held(system):
    client, resources, state = system
    def run_job(report, rc, verify=True):
        state['report_log'], state['verify_rc'] = report, rc
        body = {'role': 'summary', 'prompt': 'real work', 'sensitivity': 'public'}
        if verify:
            body['verify'] = {'cmd': 'true'}
        response = client.post('/v1/jobs', json=body)
        assert response.status_code == 202
        jid = response.json()['job_id']
        wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] == 'running')
        state['jobs'][jid] = {'state': 'exited', 'exit_code': 0, 'wd': jid}
        wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] in ('ok', 'failed'))
        wait_for(lambda: client.app.state.service.store.attempts(jid)[0]['outcome_sent'] == 1)
        return resources.get('/v1/resources/example/model').json()
    held = run_job('STATUS: ready', 1)
    assert held['stats']['last']['counts_as_failure'] == 'hold'
    assert held['level'] == 'L1'
    assert run_job('STATUS: ready', 0)['level'] == 'L1'
    assert run_job('STATUS: ready', 0)['level'] == 'L1'
    assert run_job('STATUS: ready', 0)['level'] == 'L2'
    assert run_job('STATUS: ready', 0, verify=False)['stats']['last']['counts_as_failure'] == 'hold'
    assert resources.get('/v1/resources/example/model').json()['level'] == 'L2'


def test_level_change_and_registry_reseed_preserve_stage_boundaries(system):
    _, resources, _ = system
    store = resources.app.state.service.store
    url = '/v1/resources/example/model/outcome'
    def post(trial, generation):
        response = resources.post(url, json={'job_id': trial, 'trial_id': trial, 'outcome': 'pass',
            'verify_rc': 0, 'counts_as_failure': 'no', 'level_generation': generation})
        assert response.status_code == 200
        return response.json()['resource']
    assert post('a', 0)['level'] == 'L1'
    store.apply_level_change('example/model', 'L2')
    assert store.seed([{'id': 'example/model', 'level': 'L1'}]) == 0
    stale = post('stale', 0)
    assert stale['level'] == 'L2' and stale['stats']['last']['counts_as_failure'] == 'hold'
    assert post('b', 1)['level'] == 'L2'
    assert post('c', 1)['level'] == 'L2'
    assert post('d', 1)['level'] == 'L3'


def test_attachments_reach_worker_prompt(system):
    client, _, state = system
    response = client.post('/v1/jobs', json={'role': 'summary', 'prompt': 'Read the materials',
                                            'sensitivity': 'public', 'attachments': ['registered-doc']})
    assert response.status_code == 202
    job_id = response.json()['job_id']
    wait_for(lambda: any(call['job_id'] == job_id for call in state['calls']))
    prompt = next(call['prompt'] for call in state['calls'] if call['job_id'] == job_id)
    assert 'Read the materials' in prompt
    assert 'registered-doc' in prompt

@pytest.mark.parametrize('system', ['families'], indirect=True)
def test_prior_family_persists_across_registry_change_and_capacity(system):
    client, resources, state = system
    prior = submit(client)
    wait_for(lambda: client.get('/v1/jobs/' + prior).json()['state'] == 'running')
    assert client.get('/v1/jobs/' + prior).json()['family'] == 'example'
    resource_store = resources.app.state.service.store
    with resource_store._lock:
        resource_store._conn.execute("UPDATE resources SET family='renamed' WHERE id='example/model'")
    adapter = ResourceAdapter(resources)
    adapter.post('/v1/slots/reserve', {'reservation_id': 'held-other', 'model': 'other/model'})
    body = {'role': 'second_opinion', 'prompt': 'independent', 'sensitivity': 'public', 'prior_job_id': prior}
    response = client.post('/v1/jobs', json=body)
    assert response.status_code == 202
    second = response.json()['job_id']
    wait_for(lambda: client.get('/v1/jobs/' + second).json()['reason'] == 'capacity_exhausted')
    assert not [c for c in state['calls'] if c['job_id'] == second]
    adapter.post('/v1/slots/release', {'reservation_id': 'held-other'})
    wait_for(lambda: client.get('/v1/jobs/' + second).json()['state'] == 'running')
    result = client.get('/v1/jobs/' + second).json()
    assert (result['model'], result['family']) == ('other/model', 'other')
    assert client.get('/v1/jobs/' + prior).json()['family'] == 'example'
    assert client.app.state.service.store.attempts(prior)[0]['family'] == 'example'


def test_prior_requires_confirmed_execution_and_pin_is_forbidden(system):
    client, _, state = system
    store = client.app.state.service.store
    body = {'role': 'second_opinion', 'prompt': 'independent', 'sensitivity': 'public'}
    assert client.post('/v1/jobs', json={**body, 'prior_job_id': 'job-unknown'}).status_code == 422
    legacy = store.create({'role': 'summary', 'prompt': 'legacy', 'state': 'ok'})
    store.start_attempt(legacy['id'], 'example/model')
    assert client.post('/v1/jobs', json={**body, 'prior_job_id': legacy['id']}).status_code == 422
    waiting = store.create({'role': 'summary', 'prompt': 'pending', 'state': 'launching'})
    store.start_attempt(waiting['id'], 'example/model', family='example')
    assert client.post('/v1/jobs', json={**body, 'prior_job_id': waiting['id']}).status_code == 422
    unknown = store.create({'role': 'summary', 'prompt': 'unknown family', 'state': 'running'})
    attempt = store.start_attempt(unknown['id'], 'example/model')
    store.confirm_launch(unknown['id'], attempt)
    assert client.post('/v1/jobs', json={**body, 'prior_job_id': unknown['id']}).status_code == 422
    assert client.post('/v1/jobs', json={**body, '_excluded_family': 'other'}).status_code == 422
    assert client.post('/v1/jobs', json={**body, 'pin_model': 'other/model'}).status_code == 400
    assert not state['calls']

@pytest.mark.parametrize('system', ['families'], indirect=True)
def test_prior_family_reservation_reselection_after_pool_restart(system, tmp_path):
    _, resources, state = system
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    path = str(tmp_path / 'family-restart.sqlite')
    adapter = ResourceAdapter(resources)
    class Launcher:
        def job_start(self, req):
            state['calls'].append(req)
            return {'job_id': req['job_id']}
        def job_status(self, jid):
            return {'state': 'running'}
    def build():
        return pool.build(pool.Config(db=path), resources=adapter, control=Control(),
                          launcher=Launcher(), run_heartbeat_loop=False, emit=lambda *a, **k: None)
    core, _, service = build()
    service.apply_effective({'mode': 'run', 'revision': 1, 'knobs': {}})
    with TestClient(core, headers={'Authorization': 'Bearer test-intake'}) as client:
        prior = submit(client)
        service.dispatcher.launch(service.store.get(prior))
        wait_for(lambda: client.get('/v1/jobs/' + prior).json()['state'] == 'running')
        assert client.get('/v1/jobs/' + prior).json()['family'] == 'example'
        adapter.post('/v1/slots/release', {'reservation_id': prior})
        response = client.post('/v1/jobs', json={'role': 'second_opinion', 'prompt': 'review', 'sensitivity': 'public', 'prior_job_id': prior})
        assert response.status_code == 202
        jid = response.json()['job_id']
        adapter.post('/v1/slots/reserve', {'reservation_id': jid, 'model': 'example/model-z'})
        with service.store._lock:
            service.store._conn.execute("UPDATE jobs SET state='reserving', model='example/model-z' WHERE id=?", (jid,))
    service.store.close()
    core, _, restarted = build()
    restarted.apply_effective({'mode': 'run', 'revision': 1, 'knobs': {}})
    restarted.dispatcher.launch(restarted.store.get(jid))
    with TestClient(core, headers={'Authorization': 'Bearer test-intake'}) as client:
        result = client.get('/v1/jobs/' + jid).json()
        assert (result['state'], result['model'], result['family']) == ('running', 'other/model', 'other')
        assert [c['model'] for c in state['calls'] if c['job_id'] == jid] == ['other/model']
    restarted.store.close()


def test_launcher_verification_transport_fails_closed():
    module = importlib.import_module('plugins.resource.worker-pool.launcher_client')
    calls = []
    def transport(command, stdin):
        calls.append((command, stdin))
        return (0, '{"state":"running"}')
    client = module.LauncherClient(transport=transport)
    req = {'project': 'project', 'target': 'target', 'revision': 'a' * 40, 'run_id': 'run-1',
           'attempt': 'base', 'recipe_id': 'recipe', 'stage': 'verify', 'step': 0}
    assert client.job_verify_prepare('verify-job', req)['state'] == 'running'
    assert calls[0][0].endswith('job-verify-prepare verify-job')
    assert json.loads(calls[0][1]) == req
    client.job_verify_result('verify-job')
    assert calls[1] == ('ax-env-run --caller launcher job-verify-result verify-job', '')
    client.transport = lambda command, stdin: (70, '{"error":{"reason":"unavailable"}}')
    with pytest.raises(module.LauncherError):
        client.job_verify_result('verify-job')
    with pytest.raises(ValueError):
        client.job_verify_prepare('bad id', req)

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
        if path == '/v1/slots/release' and self.state and self.state.get('release_failure'):
            raise RuntimeError('release unavailable')
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
        if state.get('start_uncertain'):
            state['calls'].append(req)
            raise launcher_module.LauncherError(70, 'unavailable')
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
    if getattr(request, 'param', None) == 'manager':
        model_rows[0]['level'] = 'L3'
        model_rows[0]['family'] = 'glm'
    if getattr(request, 'param', None) == 'two':
        model_rows.append({**model_rows[0], 'id': 'example/model-z'})
    if getattr(request, 'param', None) == 'two_models':
        model_rows.append({**model_rows[0], 'id': 'other/model', 'family': 'other'})
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
                                launcher=LauncherAdapter(fake), dispatch_interval_s=3600,
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



def tick(client):
    client.app.state.service.dispatcher.tick()


def used(resources):
    return resources.get('/v1/slots').json()['models']['example/model']['used']


def state_of(client, jid):
    return client.get('/v1/jobs/' + jid).json()['state']


@pytest.mark.parametrize('initial', ['launching', 'running'])
def test_gone_and_duplicate_reconciliation(system, initial):
    client, resources, state = system
    state['start_uncertain'] = initial == 'launching'
    jid = submit(client)
    tick(client)
    wait_for(lambda: state_of(client, jid) == initial)
    assert used(resources) == 1
    state['jobs'][jid] = {'job_id': jid, 'state': 'gone', 'exit_code': None}
    tick(client)
    assert state_of(client, jid) == 'lost'
    assert used(resources) == 0
    other = submit(client)
    state['start_uncertain'] = False
    tick(client)
    wait_for(lambda: state_of(client, other) == 'running')
    tick(client)
    assert used(resources) == 1
    wait_for(lambda: state_of(client, other) == 'running')


def test_unknown_and_invalid_responses_keep_slot(system):
    client, resources, state = system
    jid = submit(client)
    tick(client)
    wait_for(lambda: state_of(client, jid) == 'running')
    state['unknown'] = True
    tick(client)
    assert state_of(client, jid) == 'running' and used(resources) == 1
    state['unknown'] = False
    for response in ({'state': 'unknown'}, {'state': 'gone', 'exit_code': None},
                     {'job_id': 'other', 'state': 'gone', 'exit_code': None},
                     {'job_id': jid, 'state': 'gone'},
                     {'job_id': jid, 'state': 'gone', 'exit_code': 1}):
        state['jobs'][jid] = response
        tick(client)
        assert state_of(client, jid) == 'running' and used(resources) == 1


def test_restart_retries_release(system):
    client, resources, state = system
    jid = submit(client)
    tick(client)
    wait_for(lambda: state_of(client, jid) == 'running')
    state['jobs'][jid] = {'job_id': jid, 'state': 'gone', 'exit_code': None}
    state['release_failure'] = True
    tick(client)
    assert state_of(client, jid) == 'lost' and used(resources) == 1
    state['release_failure'] = False
    dispatch = importlib.import_module('plugins.resource.worker-pool.dispatch')
    service = client.app.state.service
    service.dispatcher = dispatch.Dispatcher(service, service.dispatcher.launcher)
    tick(client)
    assert state_of(client, jid) == 'lost' and used(resources) == 0
    tick(client)
    assert used(resources) == 0

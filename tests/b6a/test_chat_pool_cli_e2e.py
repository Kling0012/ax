"""Public pool job through the real launcher CLI and synthetic chat runner."""
import importlib
import json

import pytest

from tests.b6a.test_dispatch_e2e import LauncherAdapter, ResourceAdapter, system, wait_for
from tests.b6b.helpers import Sandbox


@pytest.mark.parametrize('system', ['chat'], indirect=True)
def test_pool_accepts_only_inspected_tool_free_chat_result(system, tmp_path, monkeypatch):
    client, resources, _ = system
    releases = []
    original_post = ResourceAdapter.post
    def track_post(self, path, body):
        result = original_post(self, path, body)
        if path == '/v1/slots/release':
            releases.append((body['reservation_id'], result['released']))
        return result
    monkeypatch.setattr(ResourceAdapter, 'post', track_post)
    sandbox = Sandbox(tmp_path / 'host')
    error = importlib.import_module('plugins.resource.worker-pool.launcher_client').LauncherError
    launched = set()
    def call(command, jid, body=None, extra=''):
        proc = sandbox.subprocess_env_run(f'{command} {jid}' + extra, '--caller', 'launcher', stdin=json.dumps(body or {}))
        result = json.loads(proc.stdout)
        if proc.returncode:
            raise error(proc.returncode, result.get('error', {}).get('reason', 'failed'))
        return result
    def start(self, req):
        result = call('job-start', req['job_id'], req)
        launched.add(req['job_id'])
        return result
    monkeypatch.setattr(LauncherAdapter, 'job_start', start)
    monkeypatch.setattr(LauncherAdapter, 'job_status', lambda self, jid: call('job-status', jid))
    monkeypatch.setattr(LauncherAdapter, 'job_chat_result', lambda self, jid: call('job-chat-result', jid), raising=False)
    monkeypatch.setattr(LauncherAdapter, 'job_log', lambda self, jid: call('job-log', jid, extra=' 65536'))
    monkeypatch.setattr(LauncherAdapter, 'job_stop', lambda self, jid: call('job-stop', jid))
    response = client.post('/v1/jobs', json={'role': 'second_opinion', 'prompt': 'review input', 'sensitivity': 'public',
                                            'verify': 'ax:change-safety-review-v1'})
    assert response.status_code == 202
    jid = response.json()['job_id']
    wait_for(lambda: jid in launched)
    request = json.loads((sandbox.work_dir(jid) / 'request.json').read_text())
    assert set(request) == {'model', 'max_tokens', 'messages'} and 'tools' not in request
    verdict = {'verdict': 'safe', 'concerns': [], 'summary': 'A neutral summary.'}
    (sandbox.work_dir(jid) / 'response.json').write_text(json.dumps({'ok': True, 'exit_code': 0, 'json': verdict}))
    sandbox.fd.finish('ax-job-' + jid, logs='')
    wait_for(lambda: client.get('/v1/jobs/' + jid).json()['state'] in ('ok', 'failed'))
    wait_for(lambda: resources.get('/v1/slots').json()['models']['example/model']['used'] == 0)
    first = call('job-status', jid)
    second = call('job-status', jid)
    assert first == second and first['state'] == 'exited'
    assert sandbox.fd.container('ax-job-' + jid)['State']['Status'] == 'exited'
    assert (sandbox.state / 'jobs' / (jid + '.exit.json')).exists()
    assert releases == [(jid, True)]
    result = client.get('/v1/jobs/' + jid).json()
    assert result['state'] == 'ok' and result['verify']['rc'] == 0 and result['tool_free'] is True
    assert result['tool_free_evidence'] == {'job_id': jid, 'runner': 'chat-json', 'profile': 'tool-free-chat-v1',
        'request_tools_absent': True, 'inspect_verified': True, 'trust_basis': 'host_managed_runner_and_image'}
    commands = [row['argv'][0] for row in sandbox.fd.calls()]
    assert commands.index('create') < commands.index('inspect') < commands.index('start')
    assert 'rm' not in commands and 'prune' not in commands

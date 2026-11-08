import json

import pytest

from core.manager.llm import PoolLLM, LLMUnavailable, parse_json
from tests.b6a.test_dispatch_e2e import system, wait_for


@pytest.mark.parametrize('system', ['manager'], indirect=True)
@pytest.mark.parametrize('valid', [True, False])
@pytest.mark.parametrize('enveloped', [True, False])
def test_manager_decision_through_pool(system, valid, enveloped):
    client, resources, state = system
    decision = {'actions': [{'type': 'noop', 'reason': 'nothing to do'}]} if valid else {'actions': 'wrong'}
    result = json.dumps(decision) + '\nSTATUS: ready'
    state['report_log'] = json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': result}) if enveloped else result

    class Pool:
        def submit(self, body):
            response = client.post('/v1/jobs', json=body)
            assert response.status_code == 202
            return response.json()
        def job(self, job_id):
            wait_for(lambda: client.get('/v1/jobs/' + job_id).json()['state'] == 'running')
            state['jobs'][job_id] = {'state': 'exited', 'exit_code': 0}
            wait_for(lambda: client.get('/v1/jobs/' + job_id).json()['state'] in ('ok', 'failed'))
            return client.get('/v1/jobs/' + job_id).json()
        def cancel(self, job_id):
            client.post('/v1/jobs/' + job_id + '/cancel')

    llm = PoolLLM(Pool(), poll_s=.01)
    if valid:
        assert parse_json(llm.ask('decide', sensitivity='private', deadline_s=10).text) == decision
    else:
        with pytest.raises(LLMUnavailable):
            llm.ask('decide', sensitivity='private', deadline_s=10)


@pytest.mark.parametrize('system', ['manager'], indirect=True)
@pytest.mark.parametrize('case', ['valid', 'bad_json', 'bad_schema', 'error_envelope', 'validator_rejects', 'worker'])
def test_manager_json_without_status_requires_fixed_verification(system, case, monkeypatch):
    client, _, state = system
    decision = {'actions': [{'type': 'noop', 'reason': 'nothing to do'}]}
    body = json.dumps(decision) if case != 'bad_json' else '{invalid'
    if case == 'bad_schema':
        body = json.dumps({'actions': 'wrong'})
    envelope = {'type': 'result', 'subtype': 'success', 'is_error': case == 'error_envelope', 'result': body}
    state['report_log'] = json.dumps(envelope)
    checked = []
    if case == 'validator_rejects':
        from core.manager import extschemas
        def reject(name, value):
            checked.append((name, value))
            return ['forced rejection']
        monkeypatch.setattr(extschemas, 'errors', reject)
    request = {'role': 'summary' if case == 'worker' else 'manager',
               'prompt': 'decide', 'sensitivity': 'private', 'verify': 'ax:manager-decision-v1'}
    response = client.post('/v1/jobs', json=request)
    assert response.status_code == 202, response.text
    job_id = response.json()['job_id']
    wait_for(lambda: client.get('/v1/jobs/' + job_id).json()['state'] == 'running')
    state['jobs'][job_id] = {'state': 'exited', 'exit_code': 0}
    wait_for(lambda: client.get('/v1/jobs/' + job_id).json()['state'] in ('ok', 'failed'))
    result = client.get('/v1/jobs/' + job_id).json()
    if case == 'valid':
        assert result['state'] == 'ok' and result['verify']['rc'] == 0
        assert json.loads(result['report']['text']) == decision
    else:
        assert result['state'] == 'failed'
        if case in ('bad_schema', 'validator_rejects'):
            assert result['verify']['rc'] == 1
        else:
            assert result['verify']['rc'] is None
    if case == 'validator_rejects':
        assert checked == [('manager-decision', decision)]


@pytest.mark.parametrize('system', ['manager'], indirect=True)
@pytest.mark.parametrize('case', ['valid', 'bad_json', 'bad_schema', 'error_envelope', 'validator_rejects'])
def test_manager_through_real_launcher(system, tmp_path, monkeypatch, case):
    import importlib
    from tests.b6a.test_dispatch_e2e import LauncherAdapter
    from tests.b6b.helpers import Sandbox

    client, _, _ = system
    sandbox_dir = tmp_path / 'launcher-manager'
    sandbox_dir.mkdir()
    sb = Sandbox(sandbox_dir)
    launcher_module = importlib.import_module('plugins.resource.worker-pool.launcher_client')
    launched = set()

    def call(sub, job, request=None, *args):
        code, result = sb.call(sub, job, *args, stdin=request)
        if code:
            raise launcher_module.LauncherError(code, result.get('error', {}).get('reason', 'error'))
        return result

    def start(self, req):
        result = call('job-start', req['job_id'], req)
        launched.add(req['job_id'])
        return result

    monkeypatch.setattr(LauncherAdapter, 'job_start', start)
    monkeypatch.setattr(LauncherAdapter, 'job_status', lambda self, jid: call('job-status', jid))
    monkeypatch.setattr(LauncherAdapter, 'job_log', lambda self, jid: call('job-log', jid, None, '65536'))
    monkeypatch.setattr(LauncherAdapter, 'job_stop', lambda self, jid: call('job-stop', jid))
    checked = []
    if case == 'validator_rejects':
        from core.manager import extschemas
        def reject(name, value):
            checked.append(name)
            return ['forced rejection']
        monkeypatch.setattr(extschemas, 'errors', reject)
    decision = {'actions': [{'type': 'noop', 'reason': 'nothing to do'}]}
    result_text = '{invalid' if case == 'bad_json' else json.dumps(
        {'actions': 'wrong'} if case == 'bad_schema' else decision)
    log = json.dumps({'type': 'result', 'subtype': 'success',
                      'is_error': case == 'error_envelope', 'result': result_text})
    response = client.post('/v1/jobs', json={'role': 'manager', 'prompt': 'decide',
                           'sensitivity': 'private', 'verify': 'ax:manager-decision-v1'})
    assert response.status_code == 202
    job_id = response.json()['job_id']
    wait_for(lambda: job_id in launched)
    sb.fd.finish('ax-job-' + job_id, logs=log)
    wait_for(lambda: client.get('/v1/jobs/' + job_id).json()['state'] in ('ok', 'failed'))
    job = client.get('/v1/jobs/' + job_id).json()
    assert job['state'] == ('ok' if case == 'valid' else 'failed')
    assert job['verify']['rc'] == (0 if case == 'valid' else 1 if case in ('bad_schema', 'validator_rejects') else None)
    if case == 'valid':
        assert json.loads(job['report']['text']) == decision
    if case == 'validator_rejects':
        assert checked == ['manager-decision']

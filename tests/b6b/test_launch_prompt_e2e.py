import json
import stat


def invoke(sb, command, request=None):
    result = sb.subprocess_env_run(command, '--caller', 'launcher', stdin=json.dumps(request or {}))
    return result.returncode, json.loads(result.stdout)


def test_launcher_prompt_and_log_through_command(sb):
    req = {'job_id': 'j1', 'runner': 'agent-cli', 'model': 'sample/model',
           'allow_hosts': [], 'budget_min': 5, 'prompt': 'hello world'}
    code, body = invoke(sb, 'job-start j1', req)
    assert code == 0, body
    prompt = sb.work_dir('j1') / '.ax-prompt' / 'user.md'
    assert prompt.read_text() == 'hello world'
    assert stat.S_IMODE(prompt.stat().st_mode) == 0o600
    sb.fd.finish('ax-job-j1', logs='hello from container\n')
    assert invoke(sb, 'job-status j1')[1]['state'] == 'exited'
    assert invoke(sb, 'job-log j1 10')[1]['log'] == 'container\n'


def test_launcher_rejects_empty_and_oversized_prompts(sb):
    req = {'job_id': 'j1', 'runner': 'agent-cli', 'model': 'sample/model',
           'allow_hosts': [], 'budget_min': 5}
    for value in (None, '', ' ', 'あ' * 50000):
        payload = dict(req)
        if value is not None:
            payload['prompt'] = value
        code, body = invoke(sb, 'job-start j1', payload)
        assert code == 2 and body['error']['reason'] == 'bad_prompt'
    assert not sb.run_calls()

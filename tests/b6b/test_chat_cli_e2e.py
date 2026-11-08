"""Public launcher CLI using a synthetic chat runner and fake Docker."""
import json

from tests.b6b.helpers import Sandbox


def invoke(sb, command, body=None):
    result = sb.subprocess_env_run(command, '--caller', 'launcher', stdin=json.dumps(body or {}))
    return result.returncode, json.loads(result.stdout)


def test_chat_json_through_public_launcher_cli(tmp_path):
    sb = Sandbox(tmp_path)
    request = {'job_id': 'chat-one', 'runner': 'chat-json', 'model': 'example/model',
               'prompt': 'Evaluate the summary', 'allow_hosts': [], 'budget_min': 5, 'safety_profile': 'tool-free-chat-v1'}
    code, _ = invoke(sb, 'job-start chat-one', request)
    assert code == 0
    work = sb.work_dir('chat-one')
    assert json.loads((work / 'request.json').read_text()) == {'model': 'example/model', 'max_tokens': 4096,
         'messages': [{'role': 'user', 'content': 'Evaluate the summary'}]}
    (work / 'response.json').write_text(json.dumps({'ok': True, 'exit_code': 0, 'json': {'safe': True}}))
    assert invoke(sb, 'job-chat-result chat-one')[0] != 0
    sb.fd.finish('ax-job-chat-one', logs='')
    assert invoke(sb, 'job-status chat-one')[1]['state'] == 'exited'
    code, result = invoke(sb, 'job-chat-result chat-one')
    assert code == 0 and result['job_id'] == 'chat-one' and result['json'] == {'safe': True}
    assert result['tool_free'] is True and result['tool_free_evidence'] == {'job_id': 'chat-one', 'runner': 'chat-json', 'profile': 'tool-free-chat-v1', 'request_tools_absent': True, 'inspect_verified': True, 'trust_basis': 'host_managed_runner_and_image'}
    commands = [call['argv'][0] for call in sb.fd.calls()]
    assert commands.index('create') < commands.index('inspect') < commands.index('start')
    assert invoke(sb, 'job-chat-result unknown')[0] != 0


def test_chat_isolation_mismatch_never_starts(tmp_path):
    sb = Sandbox(tmp_path)
    sb.env['FAKE_DOCKER_BREAK'] = 'network'
    request = {'job_id': 'chat-bad', 'runner': 'chat-json', 'model': 'example/model',
               'prompt': 'untrusted', 'allow_hosts': [], 'budget_min': 5, 'safety_profile': 'tool-free-chat-v1'}
    code, _ = invoke(sb, 'job-start chat-bad', request)
    assert code != 0
    commands = [call['argv'][0] for call in sb.fd.calls()]
    assert 'create' in commands and 'inspect' in commands and 'start' not in commands
    assert invoke(sb, 'job-chat-result chat-bad')[0] != 0


def test_old_host_rejects_safety_profile_before_container_creation(tmp_path):
    import os
    import subprocess
    import shutil
    from tests.b6b.helpers import ENV_RUN

    sb = Sandbox(tmp_path)
    # Frozen public job-start contract from before safety_profile was added.
    # It runs as a separate forced-command CLI, independent of any stage checkout.
    old_root = tmp_path / 'old-host'
    (old_root / 'bin').mkdir(parents=True)
    (old_root / 'lib').mkdir()
    old_cli = old_root / 'bin' / 'ax-env-run'
    shutil.copy2(ENV_RUN, old_cli)
    old_launcher = old_root / 'lib' / 'ax_launcher.py'
    old_launcher.write_text("""\
import json
import os
import sys

if sys.argv[1:] != ['--caller', 'launcher'] or os.environ.get('SSH_ORIGINAL_COMMAND') != 'job-start old-host':
    sys.exit(2)
request = json.load(sys.stdin)
allowed = {'job_id', 'runner', 'model', 'allow_hosts', 'budget_min', 'env_names', 'target_ro', 'ro_mounts',
           'session_id', 'resume_from', 'workdir_id', 'verify', 'tools', 'prompt', 'snapshot_id'}
unknown = sorted(set(request) - allowed)
if unknown:
    print(json.dumps({'ok': False, 'error': {'class': 'rejected', 'reason': 'unknown_field'}}))
    sys.exit(2)
print(json.dumps({'ok': True, 'accepted': True}))
""", encoding='utf-8')
    request = {'job_id': 'old-host', 'runner': 'chat-json', 'model': 'example/model', 'prompt': 'untrusted',
               'allow_hosts': [], 'budget_min': 5}
    environment = {**sb.env, 'SSH_ORIGINAL_COMMAND': 'job-start old-host'}
    def invoke_old(body):
        return subprocess.run([str(old_cli), '--caller', 'launcher'], env=environment,
                              input=json.dumps(body), text=True, capture_output=True)
    baseline = invoke_old(request)
    assert baseline.returncode == 0 and json.loads(baseline.stdout)['accepted'] is True
    rejected = invoke_old({**request, 'safety_profile': 'tool-free-chat-v1'})
    assert rejected.returncode == 2 and json.loads(rejected.stdout)['error']['reason'] == 'unknown_field'
    assert not any(call['argv'][0] in ('create', 'run', 'start') for call in sb.fd.calls())

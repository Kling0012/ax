"""Public job status preserves completed chat containers and records."""
import json

from tests.b6b.helpers import Sandbox


def test_repeated_chat_status_preserves_container_and_exit_record(tmp_path):
    sb = Sandbox(tmp_path)
    def invoke(command, body=None):
        result = sb.subprocess_env_run(command, '--caller', 'launcher', stdin=json.dumps(body or {}))
        return result.returncode, json.loads(result.stdout)
    request = {'job_id': 'keep-chat', 'runner': 'chat-json', 'model': 'example/model',
               'prompt': 'test', 'allow_hosts': [], 'budget_min': 5, 'safety_profile': 'tool-free-chat-v1'}
    assert invoke('job-start keep-chat', request)[0] == 0
    sb.fd.finish('ax-job-keep-chat', logs='output')
    code, first = invoke('job-status keep-chat')
    assert code == 0 and first['state'] == 'exited'
    record = sb.state / 'jobs' / 'keep-chat.exit.json'
    assert json.loads(record.read_text()) == first
    assert invoke('job-status keep-chat') == (0, first)
    assert sb.fd.container('ax-job-keep-chat')['State']['Status'] == 'exited'
    assert json.loads(record.read_text()) == first
    calls = [item['argv'] for item in sb.fd.calls()]
    assert sum(argv[0] == 'logs' for argv in calls) == 1
    assert not any(argv[0] in ('rm', 'prune') or argv[:2] in (['container', 'prune'], ['system', 'prune']) for argv in calls)

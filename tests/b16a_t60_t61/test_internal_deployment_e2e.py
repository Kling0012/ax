import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml
from starlette.testclient import TestClient

from core.resources.app import Config, build
from runtime.subloop.store import Store
from runtime.timer_runner.__main__ import ConversationFactory
from runtime.timer_runner.runner import TimerRunner, load_handler

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('host,address,allowed', [
    ('registered.invalid', '127.0.0.1', False),
    ('embedder', '8.8.8.8', False),
    ('embedder', 'mixed', False),
    ('embedder', '10.1.2.3', True),
    ('node.example.ts.net', '100.64.0.9', True),
    ('127.0.0.1', '127.0.0.1', True),
    ('100.64.0.9', '100.64.0.9', True),
    ('192.168.1.2', '192.168.1.2', True),
    ('8.8.8.8', '8.8.8.8', False),
])
def test_timer_outbound_policy_and_isolation(tmp_path, monkeypatch, host, address, allowed):
    history = tmp_path / 'history'
    history.mkdir()
    transcript = history / 'one.jsonl'
    transcript.write_text(json.dumps({'type': 'user', 'message': {'content': 'ordinary text'}}) + '\n')
    for name, contents in [('index', 'salt\n'), ('pii', 'private-person\n'), ('token', 'fake-token')]:
        (tmp_path / name).write_text(contents)
    endpoint = f'http://{host}/v1/embeddings'
    models = tmp_path / 'models.yaml'
    models.write_text(yaml.safe_dump({'models': [{'id': 'provider/model', 'kind': 'model',
        'family': 'provider', 'tier': 'standard', 'status': 'active', 'embedding_endpoint': endpoint}]}))
    core, _, _ = build(Config(db=str(tmp_path / 'resources.db'), models_file=str(models)), run_heartbeat_loop=False)
    env = {'AX_CONVERSATION_HISTORY_DIR': str(history), 'AX_EMBEDDING_PROVIDER': 'provider',
           'AX_EMBEDDING_MODEL': 'model', 'AX_EMBEDDING_URL': endpoint,
           'AX_EMBEDDING_TOKEN_FILE': str(tmp_path / 'token'), 'AX_EMBEDDING_DIMENSIONS': '1',
           'AX_REDACT_INDEX': str(tmp_path / 'index'), 'AX_PII_LIST': str(tmp_path / 'pii')}
    resolved = ['10.1.2.3', '8.8.8.8'] if address == 'mixed' else [address]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80)) for ip in resolved])
    requests = []
    redirect = []
    def embedding(request):
        requests.append(request)
        if redirect:
            return httpx.Response(302, headers={'location': 'https://external.invalid/v1/embeddings'})
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [0.5]}]})
    class Sink:
        def emit(self, *args, **kwargs):
            pass
    handler = load_handler(ROOT / 'plugins/timer/conversation-index')
    index_type = handler.__globals__['ConversationIndex']
    store = Store(tmp_path / 'timer.db')
    other_runs = []
    async def other(ctx):
        other_runs.append(True)
        return {'result': 'ok'}
    with TestClient(core) as api:
        def resources(method, url, headers, body, timeout):
            response = api.request(method, url, headers=headers, content=body)
            return response.status_code, dict(response.headers), response.content
        factory = ConversationFactory(tmp_path, index_type, environ=env, transport=resources,
                                      embedding_transport=httpx.MockTransport(embedding))
        timer = TimerRunner('timer-runner', store, Sink(), {'conversation-index': handler, 'other': other},
            {'conversation-index': {'id': 'conversation-index', 'kind': 'timer'},
             'other': {'id': 'other', 'kind': 'timer'}}, lambda *args: None,
            extra_clients={'conversation-index': {'conversation_index': factory}})
        timer.set_effective('conversation-index', {'mode': 'run', 'revision': 1, 'knobs': {'enabled': True}})
        async def run(pid):
            rid = await timer.run_job(pid, '2026-11-08T00:00:00Z')
            return store.get_run(rid)['state']
        assert asyncio.run(run('conversation-index')) == ('completed' if allowed else 'failed')
        assert asyncio.run(run('other')) == 'completed'
        assert other_runs == [True]
        assert len(requests) == int(allowed)
        if allowed:
            assert requests[0].url.host == address
            assert requests[0].headers['host'] == host
            assert json.loads(requests[0].content)['input'] == 'ordinary text'
            for sensitive in ('private-person', 'sk-' + 'C' * 25):
                transcript.write_text(json.dumps({'type': 'user', 'message': {'content': sensitive}}) + '\n')
                assert asyncio.run(run('conversation-index')) == 'failed'
                assert len(requests) == 1
            transcript.write_text(json.dumps({'type': 'user', 'message': {'content': 'another safe text'}}) + '\n')
            redirect.append(True)
            assert asyncio.run(run('conversation-index')) == 'failed'
            assert len(requests) == 2
            env.pop('AX_EMBEDDING_MODEL')
            assert asyncio.run(run('conversation-index')) == 'failed'
            assert asyncio.run(run('other')) == 'completed'
            env['AX_EMBEDDING_MODEL'] = 'model'
            env['AX_EMBEDDING_TOKEN_FILE'] = str(tmp_path / 'missing-token')
            assert asyncio.run(run('conversation-index')) == 'failed'
            assert len(requests) == 2
    store.close()


def test_compose_resolves_read_only_history_and_optional_configuration(tmp_path):
    overlay = tmp_path / 'hq.yaml'
    overlay.write_text('services:\n  hq:\n    image: example:local\n  archive-init:\n    image: example:local\nsecrets:\n  pii-list:\n    file: /dev/null\n')
    env = dict(PATH=os.environ['PATH'], HOME=os.environ['HOME'], AX_HOME=str(ROOT), AX_STATE_DIR=str(tmp_path), AX_SECRETS_DIR=str(tmp_path),
               AX_AUTOMATION_MAP_SOURCE='/dev/null', AX_UID='1000', AX_GID='1000', AX_HOST='example-host',
               AX_TAG='test', AX_BIND_ADDR='127.0.0.1', AX_TMP_DIR=str(tmp_path))
    for key in ('AX_CONVERSATION_HISTORY_HOST_DIR', 'AX_CONVERSATION_HISTORY_DIR',
                'AX_EMBEDDING_TOKEN_HOST_FILE', 'AX_EMBEDDING_TOKEN_FILE'):
        env.pop(key, None)
    files = ['compose.yaml', 'compose.d/b7.yaml', 'compose.d/b16a-t60.yaml',
             'compose.d/b16a-t62.yaml', str(overlay)]
    result = subprocess.run(['docker', 'compose', *[x for f in files for x in ('-f', str(ROOT / f))],
                             'config', '--format', 'json'], cwd=ROOT, env=env,
                            text=True, capture_output=True, check=True)
    service = json.loads(result.stdout)['services']['timer-runner']
    volumes = {v['target']: v for v in service['volumes']}
    assert volumes['/conversation-history']['read_only'] is True
    assert volumes['/run/ax-embedding/token']['read_only'] is True
    assert service['environment']['AX_CONVERSATION_HISTORY_DIR'] == ''
    manifest = yaml.safe_load((ROOT / 'plugins/timer/conversation-index/manifest.yaml').read_text())
    assert manifest['knobs']['enabled']['default'] is False


@pytest.mark.skipif(os.environ.get('AX_T60_DOCKER_E2E') != '1', reason='requires local Docker daemon')
def test_history_mount_rejects_writes_inside_container(tmp_path):
    import uuid
    history = tmp_path / 'history'
    history.mkdir(mode=0o755)
    transcript = history / 'session.jsonl'
    transcript.write_text('original\n')
    base = tmp_path / 'base.yaml'
    base.write_text('services:\n  timer-runner:\n    image: python:3.12-slim\n    network_mode: none\n')
    env = dict(PATH=os.environ['PATH'], HOME=os.environ['HOME'],
               AX_CONVERSATION_HISTORY_HOST_DIR=str(history),
               AX_CONVERSATION_HISTORY_DIR='/conversation-history')
    name = 'ax-t60-ro-' + uuid.uuid4().hex[:12]
    script = ("from pathlib import Path\n"
              "p=Path('/conversation-history/session.jsonl')\n"
              "assert p.read_text() == 'original\\n'\n"
              "try:\n p.write_text('changed')\n"
              "except OSError: pass\n"
              "else: raise SystemExit('history mount was writable')\n")
    subprocess.run(['sudo', '-n', 'env', f'AX_CONVERSATION_HISTORY_HOST_DIR={history}',
                    'AX_CONVERSATION_HISTORY_DIR=/conversation-history', 'docker', 'compose',
                    '-p', name, '-f', str(base), '-f',
                    str(ROOT / 'compose.d/b16a-t60.yaml'), 'run', '--no-deps', '-T',
                    '--name', name, 'timer-runner', 'python', '-c', script],
                   env=env, check=True, capture_output=True, text=True)
    assert transcript.read_text() == 'original\n'

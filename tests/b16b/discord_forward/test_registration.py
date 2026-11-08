import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from starlette.testclient import TestClient

from axcommon import schemas
from axcommon.creds import TokenTable
from core.resources.app import Config, build
from core.resources.envs.adapters.ssh_host import AuditJobExecutor
from core.resources.envs.jobs import Jobs
from runtime.subloop.store import Store
from runtime.timer_runner.runner import TimerRunner, load_handler

ROOT = Path(__file__).resolve().parents[3]


def test_manifest_and_undeclared_properties():
    manifest = yaml.safe_load((ROOT / 'plugins/timer/discord-forward/manifest.yaml').read_text())
    assert not schemas.errors('manifest', manifest)
    assert schemas.errors('manifest', {**manifest, 'unexpected': True})


def test_restart_uses_collect_only_and_does_not_expose_sender(tmp_path):
    calls = []
    async def handler(ctx):
        calls.append(set(ctx.clients))
        return {}
    class Sink:
        def emit(self, *args, **kwargs):
            pass
    path = tmp_path / 'timer.sqlite'
    store = Store(path)
    store.set_meta('effective:timer-runner', {'mode': 'run', 'revision': 1})
    store.set_meta('effective:discord-forward', {'mode': 'run', 'revision': 1})
    manifests = {'discord-forward': {'collect_only_handler': True}, 'other': {}}
    timer = TimerRunner('timer-runner', store, Sink(), {'discord-forward': handler, 'other': handler}, manifests,
                        lambda *args: 'sender', require_effective=True,
                        extra_clients={'discord-forward': {'collect_only': object(), 'discord_forward': object()}})
    asyncio.run(timer.run_job('discord-forward', '2026-11-08T00:00:00Z'))
    assert calls == [{'collect_only'}]
    timer.set_effective('discord-forward', {'mode': 'run', 'revision': 1})
    asyncio.run(timer.run_job('discord-forward', '2026-11-08T00:01:00Z'))
    assert 'discord_forward' in calls[-1]
    timer.set_effective('other', {'mode': 'collect_only', 'revision': 1})
    asyncio.run(timer.run_job('other', '2026-11-08T00:02:00Z'))
    assert len(calls) == 2
    store.close()


def test_user_posts_via_resources_http_with_fake_adapter(tmp_path):
    class Adapter:
        def submit(self, spec):
            return {'state': 'succeeded', 'exit_code': 0}
        def fetch(self, job_id, name):
            assert name == 'posts.json'
            content = b'[{"id":"1"}]'
            return [SimpleNamespace(name=name, size=len(content), content=content)]
    executor = AuditJobExecutor()
    executor._adapter = lambda env: Adapter()
    manifest = yaml.safe_load((ROOT / 'plugins/timer/discord-forward/manifest.yaml').read_text())
    env = dict(id='fake', state='available', capabilities=['x-session'], data_classes=['public'],
               max_concurrent=1, entrypoints=['x-user-posts'])
    tokens = TokenTable({'discord-forward': hashlib.sha256(b'fixture-token').hexdigest()})
    jobs = Jobs(tokens=tokens, manifests=lambda _: manifest, environments=lambda: [env], executor=executor)
    _, app, _ = build(Config(db=str(tmp_path / 'resources.sqlite')), env_jobs=jobs, run_heartbeat_loop=False)
    body = {'job_id': 'fixture-job', 'requires': ['x-session'], 'sensitivity': 'public',
            'entry': {'kind': 'entrypoint', 'name': 'x-user-posts', 'args': {'user': 'fixture', 'limit': 20, 'retweets': False}},
            'outputs': ['posts.json'], 'secrets': [], 'budget_min': 5, 'stall_min': 5,
            'irreversible': False, 'scope': 'one', 'idempotent': False, 'idempotency_key': 'fixture-job'}
    headers = {'Authorization': 'Bearer fixture-token'}
    with TestClient(app) as client:
        assert client.post('/v1/env/jobs', headers=headers, json=body).status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get('/v1/env/jobs/fixture-job', headers=headers).json()
            if result['state'] == 'succeeded':
                break
            time.sleep(.02)
        assert result['state'] == 'succeeded'
        assert json.loads(result['results']['fake']['output']['posts.json']) == [{'id': '1'}]
        bad = {**body, 'job_id': 'bad', 'entry': {**body['entry'], 'args': {**body['entry']['args'], 'user': 'x;bad'}}}
        assert client.post('/v1/env/jobs', headers=headers, json=bad).status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get('/v1/env/jobs/bad', headers=headers).json()
            if result['state'] == 'unknown':
                break
            time.sleep(.02)
        assert result['state'] == 'unknown'
        env['entrypoints'] = {'x-user-posts': {'enabled': False}}
        disabled = {**body, 'job_id': 'disabled', 'idempotency_key': 'disabled'}
        assert client.post('/v1/env/jobs', headers=headers, json=disabled).status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get('/v1/env/jobs/disabled', headers=headers).json()
            if result['state'] == 'failed':
                break
            time.sleep(.02)
        assert result['results']['fake']['error'] == 'entrypoint_unavailable'

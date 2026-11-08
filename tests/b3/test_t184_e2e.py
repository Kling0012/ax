"""T-184: timer, ledger work port and persisted timeline together."""
import asyncio
import json
from pathlib import Path

import pytest

from axcommon import timeutil
from axcommon.client import ServerError, TimelineClient
from axcommon.events import EventSink
from core.timeline.store import Store as TimelineStore
from runtime.subloop.store import Store as TimerStore
from runtime.timer_runner.runner import TimerRunner, load_handler


@pytest.fixture
def world(ledger_env, fake_clock, tmp_path):
    timeline = TimelineStore(tmp_path / 'timeline.sqlite')
    def transport(method, url, headers, body, timeout):
        event = json.loads(body)
        timeline.append(event if isinstance(event, list) else [event])
        return 200, {}, b'{}'
    tc = TimelineClient('http://timeline', transport=transport)
    ledger_env['service'].sink = EventSink(tc, 'ledger', 'ci', tmp_path / 'ledger-outbox')
    sink = EventSink(tc, 'hibi-state', 'ci', tmp_path / 'hibi-outbox', subloop='hibi-state')
    timer_store = TimerStore(tmp_path / 'timer.sqlite')
    class Ledger:
        calls = []
        fail = set()
        def put(self, path, body):
            self.calls.append(path)
            if path in self.fail:
                raise ServerError(503)
            r = ledger_env['work'].put(path, json=body, headers={'Authorization': 'Bearer ' + ledger_env['tokens']['plugins']['hibi-state']})
            if r.status_code >= 400:
                from axcommon.client import ApiError
                raise ApiError(r.status_code, r.json().get('error', {}).get('code', 'unknown'))
            return r.json()
        def post(self, path, body):
            r = ledger_env['work'].post(path, json=body, headers={'Authorization': 'Bearer ' + ledger_env['tokens']['plugins']['hibi-state']})
            assert r.status_code == 201
            return r.json()
    class Resources:
        calls = []
        state = 'succeeded'
        data = None
        def post(self, path, body):
            self.calls.append((path, body))
            return {'job_id': body['job_id']}
        def get(self, path):
            if path.endswith('state.json'):
                return self.data
            return {'state': self.state}
    resources, ledger = Resources(), Ledger()
    client_calls = []
    handler = load_handler(Path(__file__).resolve().parents[2] / 'plugins/timer/hibi-state')
    manifest = {'id': 'hibi-state', 'schedule': {'kind': 'interval', 'every_s': 60}}
    def factory(pid, name):
        client_calls.append((pid, name))
        return {'resources': resources, 'ledger': ledger, 'timeline': tc}[name]
    runner = TimerRunner('timer-runner', timer_store, sink, {'hibi-state': handler}, {'hibi-state': manifest}, factory)
    runner.client_calls = client_calls
    def payload():
        at = timeutil.format_ts(fake_clock.now())
        jst = fake_clock.now().astimezone(__import__('zoneinfo').ZoneInfo('Asia/Tokyo'))
        return {'now': {'state': 'focus', 'since': at, 'as_of': at, 'screen_title': 'FICTIONAL'},
                'day': {'date': jst.date().isoformat(), 'segments': [{'start': at, 'end': at, 'state': 'focus', 'category': 'work'}]}, 'fetched_at': at}
    def tick():
        asyncio.run(runner.run_job('hibi-state', timeutil.now_ts()))
    yield ledger_env, timeline, runner, resources, ledger, payload, tick
    timer_store.close()
    timeline.close()


def test_t184_success_unknown_invalid_and_modes(world, fake_clock, caplog):
    env, timeline, runner, resources, ledger, payload, tick = world
    resources.data = payload()
    tick()
    assert len(ledger.calls) == 2
    focus = env['core'].get('/v1/focus-state', headers={'Authorization': 'Bearer ' + env['tokens']['core']['hq']})
    assert focus.json()['state'] == 'focus'
    date = resources.data['day']['date']
    headers = {'Authorization': 'Bearer ' + env['tokens']['core']['hq']}
    assert env['core'].get('/v1/daily-state/' + date, headers=headers).json()['segments']
    from core.hq.api.service import BusinessAPI
    from core.hq.api import make_routes
    from axcommon.http import create_app
    from axcommon.redact import Redactor
    from axcommon.defaults import Defaults
    from starlette.testclient import TestClient
    class CoreLedger:
        def get(self, path):
            return env['core'].get(path, headers=headers).json()
    hq_app = create_app('test', '0', run_heartbeat_loop=False)
    hq_app.state.hq_auth = lambda request: {'allowed': True, 'via': 'tailnet', 'direct_tailnet': True}
    hq_app.state.hq_sample = False
    api = BusinessAPI(CoreLedger(), defaults=Defaults.load(), now=fake_clock.now, redactor=Redactor())
    hq_app.router.routes.extend(make_routes(api))
    hq = TestClient(hq_app)
    assert hq.get('/v1/hibi/now').json()['state'] == 'focus'
    assert all(pid == 'hibi-state' for pid, _ in runner.client_calls)
    saved = len(ledger.calls)
    fake_clock.advance(60)
    resources.data = payload()
    resources.data['now']['state'] = 'unknown'
    tick()
    assert len(ledger.calls) == saved
    fake_clock.advance(60)
    resources.data = payload()
    resources.data['unexpected'] = True
    tick()
    assert len(ledger.calls) == saved
    fake_clock.advance(60)
    resources.data = payload()
    resources.data['day']['segments'][0]['category'] = 17
    tick()
    assert len(ledger.calls) == saved
    fake_clock.advance(60)
    resources.data = payload()
    fake_clock.advance(121)
    tick()
    assert len(ledger.calls) == saved
    runner._running.add('hibi-state')
    tick()
    runner._running.clear()
    for mode in ('collect_only', 'draining'):
        runner.apply(mode, runner.revision + 1)
        before = len(resources.calls)
        tick()
        assert len(resources.calls) == before
    events = timeline.list_events(limit=200)
    results = [e['data'].get('result') for e in events if e['kind'] == 'automation.observed']
    assert 'skipped_unknown' in results and 'skipped_busy' in results and results.count('skipped_mode') == 2
    fake_clock.advance(181)
    assert hq.get('/v1/hibi/now').json()['state'] == 'unknown'
    assert 'FICTIONAL' not in repr(events) + repr([dict(row) for row in env['service'].store.query('SELECT * FROM state_history')]) + caplog.text


def test_t184_failures_recovery_and_independent_writes(world, fake_clock):
    env, timeline, runner, resources, ledger, payload, tick = world
    resources.data = payload()
    tick()
    assert len(ledger.calls) == 2
    fake_clock.advance(60)
    baseline = list(ledger.calls)
    for state in ('failed', 'timed_out', 'lost', 'failed', 'failed', 'failed'):
        resources.state = state
        tick()
        fake_clock.advance(60)
    assert ledger.calls == baseline
    assert len(resources.calls) == 7
    assert all(item[1]['idempotency_key'].startswith('hibi-state:') for item in resources.calls)
    assert len(env['service'].list_todos()) == 1
    resources.state = 'succeeded'
    resources.data = payload()
    ledger.fail.add('/v1/focus-state')
    tick()
    assert len(ledger.calls) == 4
    headers = {'Authorization': 'Bearer ' + env['tokens']['core']['hq']}
    assert env['core'].get('/v1/focus-state', headers=headers).json()['as_of'] != resources.data['now']['as_of']
    ledger.fail.clear()
    fake_clock.advance(60)
    resources.data = payload()
    tick()
    assert len(ledger.calls) == 6
    assert env['service'].list_todos() and len(env['service'].list_todos()) == 1
    fake_clock.advance(60)
    resources.data = payload()
    daily_path = '/v1/daily-state/' + resources.data['day']['date']
    ledger.fail.add(daily_path)
    tick()
    assert ledger.calls[-2:] == ['/v1/focus-state', daily_path]
    assert env['core'].get('/v1/focus-state', headers=headers).json()['as_of'] == resources.data['now']['as_of']
    assert env['core'].get('/v1/focus-state', headers={'Authorization': 'Bearer ' + env['tokens']['core']['hq']}).json()['state'] == 'focus'


def test_all_run_keeps_its_original_registered_targets(control_env):
    import yaml
    from tests.b3.test_t5_control import INS, _in, _issue, _set_mode

    assert _issue(control_env, INS, _set_mode('all', 'run', expires_at=_in(10))).status_code == 201
    assert control_env['core'].get('/v1/plugins/issue-automation/effective').json()['mode'] == 'run'
    root = control_env['root']
    manifest = root / 'plugins/timer/tumu-watch/manifest.yaml'
    manifest.parent.mkdir(parents=True)
    manifest.write_text((Path(__file__).resolve().parents[2] / 'plugins/timer/tumu-watch/manifest.yaml').read_text())
    registry_path = root / 'plugins/registry.yaml'
    registry = yaml.safe_load(registry_path.read_text())
    registry['plugins'].append({'id': 'tumu-watch', 'kind': 'timer', 'path': 'plugins/timer/tumu-watch/manifest.yaml'})
    registry_path.write_text(yaml.safe_dump(registry))
    control_env['service'].register()
    assert control_env['core'].get('/v1/plugins/issue-automation/effective').json()['mode'] == 'run'
    assert control_env['core'].get('/v1/plugins/tumu-watch/effective').json()['mode'] == 'collect_only'
    control_env['service'].register()
    assert control_env['core'].get('/v1/plugins/issue-automation/effective').json()['mode'] == 'run'
    assert control_env['core'].get('/v1/plugins/tumu-watch/effective').json()['mode'] == 'collect_only'


def test_named_run_requires_matching_plugin_run_permission(control_env):
    import yaml
    from tests.b3.test_t5_control import MGR, USER, _in, _issue, _set_mode

    root = control_env['root']
    manifest = root / 'plugins/timer/tumu-watch/manifest.yaml'
    manifest.parent.mkdir(parents=True)
    manifest.write_text((Path(__file__).resolve().parents[2] / 'plugins/timer/tumu-watch/manifest.yaml').read_text())
    registry_path = root / 'plugins/registry.yaml'
    registry = yaml.safe_load(registry_path.read_text())
    registry['plugins'].append({'id': 'tumu-watch', 'kind': 'timer', 'path': 'plugins/timer/tumu-watch/manifest.yaml'})
    registry_path.write_text(yaml.safe_dump(registry))
    control_env['service'].register()
    body = _set_mode('tumu-watch', 'run', expires_at=_in(10))
    assert _issue(control_env, MGR, body).status_code == 403
    assert control_env['core'].get('/v1/plugins/tumu-watch/effective').json()['mode'] == 'collect_only'
    permission = {'project': 'ax', 'repo': 'ax', 'destination': 'local',
                  'operations': ['merge'], 'source_instruction': 'test', 'issued_by': 'user'}
    wrong = control_env['core'].post('/v1/permissions', json=permission, headers=USER)
    assert wrong.status_code == 201
    body['args']['permission_ids'] = [wrong.json()['id']]
    assert _issue(control_env, MGR, body).status_code == 403
    assert control_env['core'].get('/v1/plugins/tumu-watch/effective').json()['mode'] == 'collect_only'
    permission['operations'] = ['plugin.run:tumu-watch']
    allowed = control_env['core'].post('/v1/permissions', json=permission, headers=USER)
    assert allowed.status_code == 201
    body['args']['permission_ids'] = [allowed.json()['id']]
    assert _issue(control_env, MGR, body).status_code == 201
    assert control_env['core'].get('/v1/plugins/tumu-watch/effective').json()['mode'] == 'run'


def test_capability_mismatch_precedes_permission_and_preserves_mode(control_env):
    import yaml
    from tests.b3.test_t5_control import MGR, _in, _issue, _set_mode

    root = control_env['root']
    path = root / 'plugins/timer/tumu-watch/manifest.yaml'
    path.parent.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / 'plugins/timer/tumu-watch/manifest.yaml'
    manifest = yaml.safe_load(source.read_text())
    manifest['capabilities'] = ['collect_only', 'draining']
    path.write_text(yaml.safe_dump(manifest))
    registry_path = root / 'plugins/registry.yaml'
    registry = yaml.safe_load(registry_path.read_text())
    registry['plugins'].append({'id': 'tumu-watch', 'kind': 'timer', 'path': 'plugins/timer/tumu-watch/manifest.yaml'})
    registry_path.write_text(yaml.safe_dump(registry))
    control_env['service'].register()
    response = _issue(control_env, MGR, _set_mode('tumu-watch', 'run', expires_at=_in(10)))
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'capability_mismatch'
    assert control_env['core'].get('/v1/plugins/tumu-watch/effective').json()['mode'] == 'collect_only'


def test_t68_registered_paused_observes_without_calling_handler(control_env, tmp_path):
    import yaml
    from tests.b3.test_t5_control import INS, MGR, _in, _issue, _set_mode

    existing = _issue(control_env, INS, _set_mode('all', 'run', expires_at=_in(10)))
    assert existing.status_code == 201
    root = control_env['root']
    manifest_path = root / 'plugins/timer/tumu-watch/manifest.yaml'
    manifest_path.parent.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / 'plugins/timer/tumu-watch/manifest.yaml'
    manifest_path.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
    registry_path = root / 'plugins/registry.yaml'
    registry = yaml.safe_load(registry_path.read_text(encoding='utf-8'))
    registry['plugins'].append({'id': 'tumu-watch', 'kind': 'timer',
                                'path': 'plugins/timer/tumu-watch/manifest.yaml'})
    registry_path.write_text(yaml.safe_dump(registry), encoding='utf-8')
    registered = control_env['service'].register()
    assert 'tumu-watch' in registered['registered']
    response = control_env['core'].get('/v1/plugins/tumu-watch/effective')
    assert response.status_code == 200 and response.json()['mode'] == 'collect_only'
    # The job has run capability, but this request has no run permission.
    rejected = _issue(control_env, MGR, _set_mode('tumu-watch', 'run', expires_at=_in(10)))
    assert rejected.status_code == 403 and rejected.json()['error']['code'] == 'permission_ids_required'
    assert control_env['core'].get('/v1/plugins/tumu-watch/effective').json()['mode'] == 'collect_only'

    events, calls = [], []
    class Sink:
        def emit(self, kind, subject, data, **kwargs):
            events.append((kind, subject, data))
    async def forbidden(ctx):
        calls.append(ctx.plugin_id)
    def client_factory(*args):
        calls.append(args)
        raise AssertionError('client must not be created')
    store = TimerStore(tmp_path / 'paused-timer.sqlite')
    runner = TimerRunner('timer-runner', store, Sink(), {'tumu-watch': forbidden},
                         {'tumu-watch': yaml.safe_load(manifest_path.read_text())}, client_factory,
                         require_effective=True)
    try:
        runner.set_effective('timer-runner', {'mode': 'run', 'revision': 1})
        runner.set_effective('tumu-watch', response.json())
        async def tick():
            await runner.tick()
            await runner.drain()
        asyncio.run(tick())
        observed = [(subject, data['result']) for kind, subject, data in events if kind == 'automation.observed']
        assert observed == [('tumu', 'skipped_mode')]
        assert not calls
    finally:
        store.close()


def test_t184_real_resources_api(world, fake_clock, tmp_path, monkeypatch):
    import hashlib
    import time
    import yaml
    from starlette.testclient import TestClient
    from axcommon.creds import TokenTable
    from core.resources.app import Config, build
    from core.resources.envs.jobs import Jobs
    from core.resources.envs.adapters.base import Artifact
    from core.resources.envs.adapters.ssh_host import HibiJobExecutor

    env, timeline, runner, resources, ledger, payload, tick = world
    class FakeEnvironment:
        data = None
        state = 'succeeded'
        calls = []
        def submit(self, spec):
            self.calls.append(spec)
            return {'state': self.state, 'exit_code': 0 if self.state == 'succeeded' else 1}
        def fetch(self, job_id, name):
            raw = json.dumps(self.data).encode()
            return (Artifact(name, len(raw), hashlib.sha256(raw).hexdigest(), raw),)
    fake = FakeEnvironment()
    executor = HibiJobExecutor()
    executor._adapter = lambda environment: fake
    tokens = TokenTable({'hibi-state': hashlib.sha256(b'hibi-test-token').hexdigest()})
    manifest = yaml.safe_load((Path(__file__).resolve().parents[2] / 'plugins/timer/hibi-state/manifest.yaml').read_text())
    hosts = [dict(id='fake-env', state='available', capabilities=['hibi-state-read'],
                  data_classes=['private'], max_concurrent=1, entrypoints=['hibi-state'])]
    jobs = Jobs(tokens=tokens, manifests=lambda owner: manifest if owner == 'hibi-state' else None,
                environments=lambda: hosts, executor=executor)
    _, app, _ = build(Config(db=str(tmp_path / 'resources.sqlite')), env_jobs=jobs, run_heartbeat_loop=False)
    headers = {'Authorization': 'Bearer hibi-test-token'}
    with TestClient(app) as http:
        def post(path, body):
            resources.calls.append((path, body))
            response = http.post(path, json=body, headers=headers)
            assert response.status_code == 202, response.text
            return response.json()
        def get(path):
            end = time.monotonic() + 5
            while True:
                response = http.get(path, headers=headers)
                assert response.status_code == 200, response.text
                data = response.json()
                if '/artifacts/' in path or data['state'] not in ('queued', 'running'):
                    return data
                assert time.monotonic() < end
                time.sleep(.02)
        resources.post, resources.get = post, get
        fake.data = payload()
        fake.data['day']['segments'][0]['session_title'] = 'FICTIONAL'
        tick()
        assert len(ledger.calls) == 2
        assert 'FICTIONAL' not in repr(timeline.list_events(limit=200))
        assert 'FICTIONAL' not in repr([dict(row) for row in env['service'].store.query('SELECT * FROM state_history')])
        assert 'FICTIONAL' not in repr(jobs.store._conn.execute('SELECT result FROM env_job_results').fetchall())
        assert all(pid == 'hibi-state' for pid, _ in runner.client_calls)
        from core.hq.api.service import BusinessAPI
        from core.hq.api import make_routes
        from axcommon.http import create_app
        from axcommon.redact import Redactor
        from axcommon.defaults import Defaults
        class CoreLedger:
            def get(self, path):
                return env['core'].get(path, headers={'Authorization': 'Bearer ' + env['tokens']['core']['hq']}).json()
        hq_app = create_app('test', '0', run_heartbeat_loop=False)
        hq_app.state.hq_auth = lambda request: {'allowed': True, 'via': 'tailnet', 'direct_tailnet': True}
        hq_app.state.hq_sample = False
        hq_app.router.routes.extend(make_routes(BusinessAPI(CoreLedger(), defaults=Defaults.load(), now=fake_clock.now, redactor=Redactor())))
        assert TestClient(hq_app).get('/v1/hibi/now').json()['state'] == 'focus'
        initial = len(ledger.calls)
        fake_clock.advance(60)
        fake.data = payload()
        fake.data['now']['state'] = 'unknown'
        tick()
        assert len(ledger.calls) == initial
        for status in ('failed', 'timed_out', 'lost', 'failed', 'failed', 'failed'):
            fake_clock.advance(60)
            fake.state = status
            tick()
        assert len(ledger.calls) == initial
        assert len(env['service'].list_todos()) == 1
        fake.state = 'succeeded'
        fake_clock.advance(60)
        fake.data = payload()
        ledger.fail.add('/v1/focus-state')
        tick()
        assert len(ledger.calls) == initial + 2
        ledger.fail.clear()
        fake_clock.advance(60)
        fake.data = payload()
        tick()
        assert len(ledger.calls) == initial + 4
        # A real bounded transport receives a fake SSH stream whose tar header
        # declares an oversized artifact. No ledger write may follow.
        import base64
        import subprocess
        import sys
        import tarfile
        from core.resources.envs.adapters.ssh_artifacts import BoundedHibiSSHExecutor
        from host.lib.envs import Command, Connection
        header = tarfile.TarInfo('state.json')
        header.size = 64 * 1024 + 1
        encoded = base64.b64encode(header.tobuf()).decode('ascii')
        actual_popen = subprocess.Popen
        def fake_ssh(*args, **kwargs):
            code = "import base64,sys; sys.stdout.buffer.write(base64.b64decode('" + encoded + "') + b'x' * 65537)"
            return actual_popen([sys.executable, '-c', code], **kwargs)
        transport = BoundedHibiSSHExecutor(remote_id='fake-env', host='fake', user='test', port=22, credential_ref=None)
        original_fetch = fake.fetch
        def oversized_fetch(job_id, name):
            transport.execute(connection=Connection('ssh-host', 'fake-env', None), command=Command('fetch', (job_id, name)))
            raise AssertionError('oversized response accepted')
        fake.fetch = oversized_fetch
        before = len(ledger.calls)
        fake_clock.advance(60)
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, 'Popen', fake_ssh)
            tick()
        assert len(ledger.calls) == before
        job_response = http.get('/v1/env/jobs/' + fake.calls[-1]['job_id'], headers=headers).json()
        assert job_response['state'] == 'failed'
        assert next(iter(job_response['results'].values()))['error'] == 'artifact_too_large'
        # The same bounded stream accepts a valid single-file tar archive.
        import io
        from core.resources.envs.adapters.base import BaseEnvironmentAdapter
        fake_clock.advance(60)
        fake.data = payload()
        raw = json.dumps(fake.data).encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            item = tarfile.TarInfo('state.json')
            item.size = len(raw)
            archive.addfile(item, io.BytesIO(raw))
        good = base64.b64encode(buffer.getvalue()).decode('ascii')
        def good_ssh(*args, **kwargs):
            code = "import base64,sys; sys.stdout.buffer.write(base64.b64decode('" + good + "'))"
            return actual_popen([sys.executable, '-c', code], **kwargs)
        def valid_fetch(job_id, name):
            response = transport.execute(connection=Connection('ssh-host', 'fake-env', None), command=Command('fetch', (job_id, name)))
            return tuple(BaseEnvironmentAdapter._artifact(item) for item in response['artifacts'])
        fake.fetch = valid_fetch
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, 'Popen', good_ssh)
            tick()
        assert len(ledger.calls) == before + 2
        fake.fetch = original_fetch
        for mode in ('collect_only', 'draining'):
            runner.apply(mode, runner.revision + 1)
            count = len(fake.calls)
            fake_clock.advance(60)
            tick()
            assert len(fake.calls) == count
        spec = dict(fake.calls[0])
        spec['job_id'] = 'invalid-command'
        spec['entry'] = {'kind': 'entrypoint', 'name': 'hibi-state', 'args': {'command': 'anything'}}
        assert http.post('/v1/env/jobs', json=spec, headers=headers).status_code in (403, 422)
        job_id = fake.calls[0]['job_id']
        assert http.get('/v1/env/jobs/' + job_id + '/artifacts/other.json', headers=headers).status_code == 404
        assert http.get('/v1/env/jobs/..%2Fbad/artifacts/state.json', headers=headers).status_code in (404, 422)

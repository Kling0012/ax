import asyncio
import json
import subprocess

import yaml

from runtime.subloop.store import Store
from runtime.timer_runner.__main__ import SecurityWatchFactory, TumuWatchFactory
from runtime.timer_runner.runner import TimerRunner, load_handler


def test_registered_monitors_require_explicit_tumu_run_and_isolate_missing_inputs(tmp_path, repo_root):
    manifests = {name: yaml.safe_load((repo_root / 'plugins/timer' / name / 'manifest.yaml').read_text())
                 for name in ('tumu-watch', 'security-watch')}
    assert manifests['tumu-watch']['default_mode'] == 'collect_only'
    assert 'run' in manifests['tumu-watch']['capabilities']
    events = []
    class Sink:
        def emit(self, kind, subject, data=None, **kwargs):
            events.append((kind, subject, data))
    calls = []
    class Reader:
        def read(self, target):
            calls.append(target)
            return {'status': 'ok', 'stalled': False}
    store = Store(tmp_path / 'timer.sqlite')
    handlers = {name: load_handler(repo_root / 'plugins/timer' / name) for name in manifests}
    tumu = TumuWatchFactory(tmp_path, environ={'AX_TUMU_TARGETS_FILE': str(tmp_path / 'missing')})
    security = SecurityWatchFactory(tmp_path, environ={'AX_SECURITY_BASELINES_FILE': str(tmp_path / 'missing')},
                                    token_reader=lambda _: 'test-token')
    timer = TimerRunner('timer-runner', store, Sink(), handlers, manifests, lambda *args: None,
                        require_effective=True, extra_clients={
                            'tumu-watch': {'tumu_watch': tumu},
                            'security-watch': {'security_watch': security}})
    async def exercise():
        timer.set_effective('timer-runner', {'mode': 'run', 'revision': 1})
        timer.set_effective('tumu-watch', {'mode': 'collect_only', 'revision': 1})
        timer.set_effective('security-watch', {'mode': 'run', 'revision': 1})
        skipped = await timer.run_job('tumu-watch', '2026-11-08T00:00:00Z')
        failed = await timer.run_job('security-watch', '2026-11-08T00:00:00Z')
        timer.set_effective('tumu-watch', {'mode': 'run', 'revision': 2})
        unavailable = await timer.run_job('tumu-watch', '2026-11-08T00:01:00Z')
        assert calls == []
        targets = tmp_path / 'targets.json'
        targets.write_text(json.dumps({'example': {}}))
        timer.extra_clients['tumu-watch']['tumu_watch'] = TumuWatchFactory(
            tmp_path, environ={'AX_TUMU_TARGETS_FILE': str(targets)}, reader=Reader())
        ran = await timer.run_job('tumu-watch', '2026-11-08T00:02:00Z')
        return skipped, failed, unavailable, ran
    skipped, failed, unavailable, ran = asyncio.run(exercise())
    assert skipped is None
    assert store.get_run(failed)['reason'] == 'SecurityBaselineUnavailable'
    assert store.get_run(failed)['state'] == 'failed'
    assert calls == ['example']
    assert store.get_run(ran)['state'] == 'completed'
    assert store.get_run(unavailable)['state'] == 'completed'
    assert any(kind == 'run.state' and subject == 'tumu-watch' and data.get('result') == 'unavailable'
               for kind, subject, data in events)
    store.close()


def test_monitor_compose_and_ssh_policy(repo_root):
    result = subprocess.run(['docker', 'compose', '-f', 'compose.yaml', '-f', 'compose.d/b7.yaml',
                             '-f', 'compose.d/b16a-security.yaml', '-f',
                             'compose.d/z-b16a-security-inputs.yaml', 'config', '--format', 'json'],
                            cwd=repo_root, env={'PATH': '/usr/bin:/bin', 'AX_STATE_DIR': '/state',
                                               'AX_SECRETS_DIR': '/secrets', 'AX_HOME': str(repo_root),
                                               'AX_TAG': 'test', 'AX_UID': '1000', 'AX_GID': '1000',
                                               'AX_TMP_DIR': '/state/tmp', 'AX_HOST': 'test'},
                            check=True, capture_output=True, text=True)
    service = json.loads(result.stdout)['services']['timer-runner']
    assert service['environment']['AX_TUMU_TARGETS_FILE'] == '/run/ax-monitor-inputs/tumu-targets.json'
    assert service['environment']['AX_SECURITY_BASELINES_FILE'] == '/run/ax-monitor-inputs/security-baselines.json'
    mounts = {v['target']: v for v in service['volumes']}
    for path in ('/run/ax-monitor-inputs', '/run/ax-tumu-secrets', '/etc/ssh/ssh_config'):
        assert mounts[path]['read_only'] is True
    result = subprocess.run(['ssh', '-G', '-F', str(repo_root / 'plugins/timer/tumu-watch/ssh_config'),
                             'example.invalid'], check=True, capture_output=True, text=True)
    assert 'stricthostkeychecking true' in result.stdout
    assert 'userknownhostsfile /run/ax-tumu-secrets/known_hosts' in result.stdout
    assert 'globalknownhostsfile /dev/null' in result.stdout

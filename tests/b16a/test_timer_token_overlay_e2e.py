"""Exercise the host-facing overlay generator without exposing credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_registry_controls_timer_credentials_without_disclosing_values(tmp_path):
    registry = tmp_path / 'registry.yaml'
    registry.write_text(yaml.safe_dump({'plugins': [
        {'id': 'hibi-state', 'kind': 'timer'},
        {'id': 'new-timer', 'kind': 'timer'},
        {'id': 'other', 'kind': 'resource'},
    ]}))
    secrets = tmp_path / 'secrets'
    tokens = secrets / 'plugin-tokens'
    tokens.mkdir(parents=True)
    sentinel = 'credential-value-must-never-be-logged'
    for name in ('hibi-state', 'new-timer', 'other'):
        (tokens / name).write_text(sentinel + name)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, '-m', 'runtime.timer_runner.token_compose', str(registry), str(secrets)],
                            cwd=ROOT, env=env, text=True, capture_output=True, check=True)
    doc = json.loads(result.stdout)
    assert doc['services']['timer-runner']['secrets'] == ['plugin-token-hibi-state', 'plugin-token-new-timer']
    assert set(doc['secrets']) == {'plugin-token-hibi-state', 'plugin-token-new-timer'}
    assert doc['secrets']['plugin-token-new-timer']['file'] == str(tokens / 'new-timer')
    assert sentinel not in result.stdout + result.stderr
    registry.write_text(yaml.safe_dump({'plugins': [{'id': '../escape', 'kind': 'timer'}]}))
    invalid = subprocess.run([sys.executable, '-m', 'runtime.timer_runner.token_compose', str(registry), str(secrets)],
                             cwd=ROOT, env=env, text=True, capture_output=True)
    assert invalid.returncode != 0
    assert sentinel not in invalid.stdout + invalid.stderr


def test_timer_modes_skip_work_and_failure_logs_hide_token(tmp_path, caplog):
    import asyncio
    from axcommon import timeutil
    from runtime.subloop.store import Store
    from runtime.timer_runner.runner import TimerRunner

    token = 'secret-value-not-for-logs'
    events = []
    jobs = []
    client_calls = []
    class Sink:
        def emit(self, kind, subject, data, **kwargs):
            events.append((kind, data))
    async def handler(ctx):
        jobs.append(ctx.plugin_id)
        raise RuntimeError(token)
    def factory(pid, name):
        assert pid == 'hibi-state'
        client_calls.append(name)
        return object()
    store = Store(tmp_path / 'timer.sqlite')
    runner = TimerRunner('timer-runner', store, Sink(), {'hibi-state': handler},
                         {'hibi-state': {'id': 'hibi-state'}}, factory)
    try:
        for mode in ('collect_only', 'draining'):
            runner.apply(mode, runner.revision + 1)
            before = len(events)
            asyncio.run(runner.run_job('hibi-state', timeutil.now_ts()))
            observations = [data['result'] for kind, data in events[before:] if kind == 'automation.observed']
            assert observations == ['skipped_mode']
            assert not jobs and not client_calls
        runner.apply('run', runner.revision + 1)
        asyncio.run(runner.run_job('hibi-state', timeutil.now_ts()))
        assert jobs == ['hibi-state']
        assert token not in caplog.text
        assert token not in repr(events)
    finally:
        store.close()


def test_scoped_timer_manifest_adds_separate_credential(tmp_path):
    root = tmp_path / 'ax'
    registry = root / 'plugins' / 'registry.yaml'
    manifest_path = root / 'plugins' / 'timer' / 'digest' / 'manifest.yaml'
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(yaml.safe_dump({'id': 'digest', 'memory_capabilities': ['memory.read']}))
    registry.write_text(yaml.safe_dump({'plugins': [{'id': 'digest', 'kind': 'timer',
        'path': 'plugins/timer/digest/manifest.yaml'}]}))
    result = subprocess.run([sys.executable, '-m', 'runtime.timer_runner.token_compose', str(registry), str(tmp_path / 'secrets')],
                            cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)), text=True, capture_output=True, check=True)
    doc = json.loads(result.stdout)
    assert doc['secrets']['plugin-token-memory-read--digest']['file'] == str(tmp_path / 'secrets/plugin-tokens/memory-read--digest')
    assert 'plugin-token-digest' in doc['secrets']

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from core.resources.envs.adapters.ssh_artifacts import ArtifactSSHExecutor
from core.resources.envs.adapters.ssh_host import SSHHostAdapter

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / 'host/env-module-min/releases/b6/bin/ax-env-run'


@pytest.fixture
def client(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    sudo = bindir / 'sudo'
    sudo.write_text('#!/bin/sh\n[ "$1 $2 $3" = "-n sshd -T" ] || exit 92\n[ -z "${AUDIT_COUNT:-}" ] || printf "run\\n" >> "$AUDIT_COUNT"\n[ "${FAIL_AUDIT:-0}" = 0 ] || exit 1\nprintf "passwordauthentication no\\npermitrootlogin no\\n"\n')
    sudo.chmod(0o755)
    env = dict(os.environ, AX_ENV_HOME=str(tmp_path / 'env'), PATH=str(bindir) + ':' + os.environ['PATH'])
    def runner(argv, **kwargs):
        operation = next((i for i, value in enumerate(argv) if value in ('submit', 'status', 'fetch')), None)
        return subprocess.run([str(ENTRY), *argv[operation:]], input=kwargs['input'], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    environment = {'id': 'remote', 'adapter': 'ssh-host', 'state': 'available',
                   'connect': {'kind': 'ssh', 'host': 'host', 'user': 'user', 'port': 22},
                   'conn_credential': {'slot': 'env-ssh-remote', 'place': 'secrets_dir/env-ssh-remote'},
                   'secret_targets': []}
    adapter = SSHHostAdapter.from_environment(environment, secrets_dir=str(tmp_path), runner=runner)
    return adapter, env, runner


def spec(job='job-1'):
    return {'job_id': job, 'idempotency_key': job, 'entry': {'kind': 'entrypoint', 'name': 'audit-readonly', 'args': {}}}


def test_roundtrip(client):
    adapter, _, _ = client
    assert adapter.submit(spec())['state'] == 'succeeded'
    assert adapter.status('job-1')['exit_code'] == 0
    artifacts = adapter.fetch('job-1')
    assert len(artifacts) == 1 and artifacts[0].name == 'audit.tsv'
    assert b'sshd_effective\tpasswordauthentication\tno' in artifacts[0].content


def test_sudo_failure_is_unavailable(client):
    adapter, env, _ = client
    env['FAIL_AUDIT'] = '1'
    assert adapter.submit(spec())['state'] == 'failed'
    assert adapter.status('job-1')['error']['class'] == 'unavailable'
    assert b'COVERAGE\tunavailable' in adapter.fetch('job-1')[0].content


@pytest.mark.parametrize('entry', [
    {'kind': 'entrypoint', 'name': 'audit-readonly', 'args': {'command': 'touch /x'}},
    {'kind': 'entrypoint', 'name': 'audit-readonly', 'args': {'x': ';id'}},
    {'kind': 'entrypoint', 'name': 'rm', 'args': {}},
])
def test_rejects_commands_and_invalid_specs(client, entry):
    adapter, _, _ = client
    job = spec()
    job['entry'] = entry
    with pytest.raises(ValueError):
        adapter.submit(job)


def test_unknown_and_unsafe_jobs_fail(client):
    adapter, _, runner = client
    with pytest.raises(ValueError):
        adapter.status('missing')
    with pytest.raises(ValueError):
        adapter.fetch('missing')
    result = runner(['status', '../escape'], input=b'')
    assert result.returncode == 2


def test_idempotent_replay_and_conflict(client, tmp_path):
    adapter, env, _ = client
    count = tmp_path / 'runs'
    env['AUDIT_COUNT'] = str(count)
    first = adapter.submit(spec())
    assert adapter.submit(spec()) == first
    assert count.read_text().splitlines() == ['run']
    changed = spec()
    changed['sensitivity'] = 'private'
    with pytest.raises(ValueError):
        adapter.submit(changed)
    assert count.read_text().splitlines() == ['run']


def test_interrupted_state_is_unknown_without_reexecution(client, tmp_path):
    adapter, env, runner = client
    count = tmp_path / 'runs'
    env['AUDIT_COUNT'] = str(count)
    assert adapter.submit(spec())['state'] == 'succeeded'
    state = Path(env['AX_ENV_HOME']) / 'jobs' / 'job-1' / 'state.json'
    state.write_text('{', encoding='utf-8')
    assert adapter.status('job-1')['state'] == 'unknown'
    assert adapter.submit(spec())['state'] == 'unknown'
    assert count.read_text().splitlines() == ['run']


def test_registered_adapter_replay_conflict_and_interruption(client, tmp_path):
    adapter, env, _ = client
    count = tmp_path / 'registered-adapter-count'
    env['AUDIT_COUNT'] = str(count)
    assert adapter.submit(spec())['state'] == 'succeeded'
    assert adapter.submit(spec())['state'] == 'succeeded'
    changed = spec()
    changed['sensitivity'] = 'private'
    with pytest.raises(ValueError):
        adapter.submit(changed)
    state = Path(env['AX_ENV_HOME']) / 'jobs' / 'job-1' / 'state.json'
    state.write_text('{', encoding='utf-8')
    assert adapter.status('job-1')['state'] == 'unknown'
    assert adapter.submit(spec())['state'] == 'unknown'
    assert count.read_text().splitlines() == ['run']


def test_resources_api_runs_registered_readonly_audit(client, tmp_path):
    import hashlib
    from starlette.testclient import TestClient
    from axcommon.creds import TokenTable
    from core.resources.app import Config, build
    from core.resources.envs.adapters.ssh_host import AuditJobExecutor
    from core.resources.envs.jobs import Jobs
    from plugins.timer.b16a_monitor.environment_jobs import EnvironmentJobsClient
    from urllib.parse import urlsplit

    _, _, runner = client
    env = {'id': 'remote', 'adapter': 'ssh-host', 'state': 'available',
           'connect': {'kind': 'ssh', 'host': 'host', 'user': 'user', 'port': 22},
           'conn_credential': {'slot': 'env-ssh-remote', 'place': 'secrets_dir/env-ssh-remote'},
           'secret_targets': [], 'capabilities': ['security-audit'], 'data_classes': ['private'],
           'max_concurrent': 1, 'entrypoints': ['audit-readonly']}
    manifest = {'id': 'monitor', 'secrets': [], 'env_jobs': {'operations': ['submit', 'get'],
                'entrypoints': ['audit-readonly'], 'requires': ['security-audit']}}
    jobs = Jobs(tokens=TokenTable({'monitor': hashlib.sha256(b'test-token').hexdigest()}),
                manifests=lambda _: manifest, environments=lambda: [env],
                executor=AuditJobExecutor(secrets_dir=str(tmp_path), runner=runner))
    _, app, _ = build(Config(db=str(tmp_path / 'audit-resources.sqlite')), env_jobs=jobs, run_heartbeat_loop=False)
    with TestClient(app) as http:
        def transport(method, url, headers, body, timeout):
            response = http.request(method, urlsplit(url).path, headers=headers, content=body)
            return response.status_code, dict(response.headers), response.content
        monitor = EnvironmentJobsClient('http://resources:8081', 'test-token', transport=transport,
                                        poll_seconds=.02, wait_seconds=5)
        results = monitor.run(requires=['security-audit'], scope='each', entry='audit-readonly')
        assert results['remote']['status'] == 'ok'
        assert results['remote']['values']['sshd_effective']['passwordauthentication'] == 'no'


def test_resources_audit_reads_ssh_without_remote_artifacts(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    from starlette.testclient import TestClient
    from axcommon.creds import TokenTable
    from core.resources.app import Config, build
    from core.resources.envs.adapters.ssh_host import AuditJobExecutor
    from core.resources.envs.jobs import Jobs
    from plugins.timer.b16a_monitor.environment_jobs import EnvironmentJobsClient

    (tmp_path / 'env-ssh-borrowed').write_text('fake key')
    (tmp_path / 'env-ssh-borrowed-known-hosts').write_text('fake host key')
    calls = []
    offline = [False]
    # Values are synthetic; the repeated directives match the observed shape.
    response = [b'passwordauthentication no\npermitrootlogin no\n'
                b'listenaddress 192.0.2.1:22\nlistenaddress [2001:db8::1]:22\n'
                b'hostkey /fake/key1\nhostkey /fake/key2\n'
                b'acceptenv LANG\nacceptenv LC_*\n']
    def runner(argv, **kwargs):
        calls.append(argv)
        assert argv[-1] == 'sudo -n sshd -T'
        if offline[0]:
            return SimpleNamespace(returncode=255, stdout=b'', stderr=b'')
        return SimpleNamespace(returncode=0, stdout=response[0], stderr=b'')
    env = {'id': 'borrowed', 'adapter': 'ssh-host', 'state': 'available',
           'audit_transport': 'ssh-readonly',
           'connect': {'kind': 'ssh', 'host': 'fake-host', 'user': 'reader', 'port': 22},
           'conn_credential': {'slot': 'env-ssh-borrowed', 'place': 'secrets_dir/env-ssh-borrowed'},
           'secret_targets': [], 'capabilities': ['security-audit'], 'data_classes': ['private'],
           'max_concurrent': 1, 'entrypoints': ['audit-readonly']}
    manifest = {'id': 'monitor', 'secrets': [], 'env_jobs': {'operations': ['submit', 'get'],
                'entrypoints': ['audit-readonly'], 'requires': ['security-audit']}}
    jobs = Jobs(tokens=TokenTable({'monitor': hashlib.sha256(b'test-token').hexdigest()}),
                manifests=lambda _: manifest, environments=lambda: [env],
                executor=AuditJobExecutor(secrets_dir=str(tmp_path), runner=runner))
    _, app, _ = build(Config(db=str(tmp_path / 'readonly.sqlite')), env_jobs=jobs, run_heartbeat_loop=False)
    with TestClient(app) as http:
        def transport(method, url, headers, body, timeout):
            response = http.request(method, urlsplit(url).path, headers=headers, content=body)
            return response.status_code, dict(response.headers), response.content
        client = EnvironmentJobsClient('http://resources:8081', 'test-token', transport=transport,
                                       poll_seconds=.02, wait_seconds=5)
        result = client.run(requires=['security-audit'], scope='each', entry='audit-readonly')
        values = result['borrowed']['values']['sshd_effective']
        assert values == {'passwordauthentication': 'no', 'permitrootlogin': 'no',
                          'listenaddress': ['192.0.2.1:22', '[2001:db8::1]:22'],
                          'hostkey': ['/fake/key1', '/fake/key2'], 'acceptenv': ['LANG', 'LC_*']}
        # Exercise the timer's public entry point through the resources API.
        import asyncio
        from runtime.subloop.store import Store
        from runtime.timer_runner.__main__ import SecurityWatchFactory
        from runtime.timer_runner.runner import TimerRunner, load_handler

        baseline = tmp_path / 'baseline.json'
        baseline.write_text(json.dumps({'borrowed': {'version': 'v1', 'values': {'sshd_effective': values}}}))
        events = []
        class Sink:
            def emit(self, kind, subject, data=None, **kwargs):
                events.append((kind, data))
        class Ledger:
            def post(self, path, body):
                pass
        factory = SecurityWatchFactory(tmp_path, environ={'AX_SECURITY_BASELINES_FILE': str(baseline)},
                                       token_reader=lambda _: 'test-token', transport=transport)
        store = Store(tmp_path / 'timer.sqlite')
        timer = TimerRunner('timer-runner', store, Sink(),
                            {'security-watch': load_handler(ROOT / 'plugins/timer/security-watch')},
                            {'security-watch': {'id': 'security-watch', 'kind': 'timer'}},
                            lambda *args: Ledger(), extra_clients={'security-watch': {'security_watch': factory}})
        asyncio.run(timer.run_job('security-watch', '2026-11-08T00:00:00Z'))
        assert not any(kind == 'change.notification' for kind, _ in events)
        import sqlite3
        with sqlite3.connect(tmp_path / 'security-watch' / 'monitor.sqlite3') as db:
            state = json.loads(db.execute('SELECT value FROM state WHERE key = ?',
                                          ('security:borrowed',)).fetchone()[0])
        assert state['status'] == 'ok'
        response[0] = response[0].replace(b'acceptenv LC_*', b'acceptenv LC_CHANGED')
        asyncio.run(timer.run_job('security-watch', '2026-11-08T00:01:00Z'))
        assert any(kind == 'change.notification' and data['title'] == 'security-difference'
                   for kind, data in events)
        store.close()
        for broken in (b'passwordauthentication no\npasswordauthentication yes\n',
                       b'passwordauthentication no\ninvalid-line\n',
                       b'hostkey /fake/key\n', b'passwordauthentication no\n\xff'):
            response[0] = broken
            invalid = client.run(requires=['security-audit'], scope='each', entry='audit-readonly')
            assert invalid['borrowed']['status'] == 'error'
        offline[0] = True
        failed = client.run(requires=['security-audit'], scope='each', entry='audit-readonly')
    assert failed['borrowed']['status'] == 'error'
    assert len(calls) == 8


def test_readonly_audit_credentials_and_known_hosts_are_isolated(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    from starlette.testclient import TestClient
    from axcommon.creds import TokenTable
    from core.resources.app import Config, build
    from core.resources.envs.adapters.ssh_host import AuditJobExecutor
    from core.resources.envs.jobs import Jobs
    from plugins.timer.b16a_monitor.environment_jobs import EnvironmentJobsClient

    key = tmp_path / 'env-ssh-noded'
    hosts = tmp_path / 'env-ssh-noded-known-hosts'
    key.write_text('fake key')
    hosts.write_text('trusted.example fake-host-key\n')
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        assert argv[argv.index('-i') + 1] == str(key)
        assert 'StrictHostKeyChecking=yes' in argv
        assert 'GlobalKnownHostsFile=/dev/null' in argv
        assert f'UserKnownHostsFile={hosts}' in argv
        host = argv[-2].split('@', 1)[1]
        ok = any(line.split()[0] == host for line in hosts.read_text().splitlines())
        return SimpleNamespace(returncode=0 if ok else 255,
                               stdout=b'passwordauthentication no\n' if ok else b'', stderr=b'')
    def environment(identifier, host):
        return {'id': identifier, 'adapter': 'ssh-host', 'state': 'available',
                'audit_transport': 'ssh-readonly',
                'connect': {'kind': 'ssh', 'host': host, 'user': 'reader', 'port': 22},
                'conn_credential': {'slot': 'env-ssh-noded', 'place': 'secrets_dir/env-ssh-noded'},
                'secret_targets': [], 'capabilities': ['security-audit'], 'data_classes': ['private'],
                'max_concurrent': 1, 'entrypoints': ['audit-readonly']}
    envs = [environment('trusted', 'trusted.example'), environment('unknown', 'unknown.example')]
    manifest = {'id': 'monitor', 'secrets': [], 'env_jobs': {'operations': ['submit', 'get'],
                'entrypoints': ['audit-readonly'], 'requires': ['security-audit']}}
    jobs = Jobs(tokens=TokenTable({'monitor': hashlib.sha256(b'test-token').hexdigest()}),
                manifests=lambda _: manifest, environments=lambda: envs,
                executor=AuditJobExecutor(secrets_dir=str(tmp_path), runner=runner))
    _, app, _ = build(Config(db=str(tmp_path / 'audit.sqlite')), env_jobs=jobs, run_heartbeat_loop=False)
    with TestClient(app) as http:
        def transport(method, url, headers, body, timeout):
            response = http.request(method, urlsplit(url).path, headers=headers, content=body)
            return response.status_code, dict(response.headers), response.content
        client = EnvironmentJobsClient('http://resources:8081', 'test-token', transport=transport,
                                       poll_seconds=.02, wait_seconds=5)
        result = client.run(requires=['security-audit'], scope='each', entry='audit-readonly')
        assert result['trusted']['status'] == 'ok'
        assert result['unknown']['error'] == 'audit_unavailable'
        missing_dir = tmp_path / 'missing'
        missing_dir.mkdir()
        (missing_dir / 'env-ssh-noded').write_text('fake key')
        jobs.executor.secrets_dir = str(missing_dir)
        result = client.run(requires=['security-audit'], scope='each', entry='audit-readonly')
        assert all(value['error'] == 'audit_known_hosts_unavailable' for value in result.values())
        empty_dir = tmp_path / 'empty'
        empty_dir.mkdir()
        jobs.executor.secrets_dir = str(empty_dir)
        result = client.run(requires=['security-audit'], scope='each', entry='audit-readonly')
        assert all(value['error'] == 'audit_credential_unavailable' for value in result.values())
        jobs.executor.secrets_dir = str(tmp_path)
        result = client.run(requires=['security-audit'], scope='each', entry='audit-readonly')
        assert result['trusted']['status'] == 'ok'
        assert len(calls) == 4


def test_openssh_effective_configuration_uses_only_dedicated_known_hosts(tmp_path):
    from core.resources.envs.adapters.ssh_host import AuditJobExecutor
    from types import SimpleNamespace
    key = tmp_path / 'env-ssh-noded'
    hosts = tmp_path / 'env-ssh-noded-known-hosts'
    key.write_text('fake key')
    hosts.write_text('fake-host fake-host-key\n')
    def runner(argv, **kwargs):
        config = subprocess.run([argv[0], '-G', *argv[1:-1]], capture_output=True, text=True, check=True)
        assert 'stricthostkeychecking true' in config.stdout
        assert f'userknownhostsfile {hosts}' in config.stdout
        assert 'globalknownhostsfile /dev/null' in config.stdout
        return SimpleNamespace(returncode=255, stdout=b'', stderr=b'unknown host')
    env = {'id': 'noded', 'adapter': 'ssh-host', 'state': 'available',
           'audit_transport': 'ssh-readonly', 'secret_targets': [],
           'connect': {'kind': 'ssh', 'host': 'fake-host', 'user': 'reader', 'port': 22},
           'conn_credential': {'slot': 'env-ssh-noded', 'place': 'secrets_dir/env-ssh-noded'}}
    assert AuditJobExecutor(secrets_dir=str(tmp_path), runner=runner).submit(env, spec())['error'] == 'audit_unavailable'


def test_hibi_receiver_runs_only_fixed_readonly_entry(client, tmp_path):
    adapter, env, _ = client
    env['HOME'] = str(tmp_path)
    bridge_dir = tmp_path / 'dev' / 'hq-hibi-bridge'
    bridge_dir.mkdir(parents=True)
    (bridge_dir / 'bridge.py').write_text('''\ndef build_config(env): return {}\ndef hq_v1_view(value): return value\nclass Bridge:\n    def __init__(self, cfg): self.client = None\n    def now_response(self): return {"state": "unknown", "as_of": "2026-11-08T00:00:00Z", "session_title": "PRIVATE"}\n    def day_response(self, value): return {"date": "2026-11-08", "segments": []}\n''')
    job = {'job_id': 'hibi-job', 'idempotency_key': 'hibi-job',
           'entry': {'kind': 'entrypoint', 'name': 'hibi-state', 'args': {}},
           'outputs': ['state.json'], 'requires': ['hibi-state-read'],
           'secrets': [], 'budget_min': 2, 'irreversible': False}
    assert adapter.submit(job)['state'] == 'succeeded'
    content = adapter.fetch('hibi-job', 'state.json')[0].content
    assert json.loads(content)['now']['state'] == 'unknown'
    assert b'PRIVATE' not in content
    job['job_id'] = 'hibi-invalid'
    job['entry']['args'] = {'command': 'anything'}
    with pytest.raises(ValueError):
        adapter.submit(job)

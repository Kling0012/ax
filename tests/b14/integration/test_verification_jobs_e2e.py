"""Durable verification claims using the public launcher adapter interface."""
import os
import pytest

from plugins.subloop.change.integration.journal import Journal
from plugins.subloop.change.integration.ports import DependencyUnavailable
from plugins.subloop.change.integration.verification_jobs import VerificationJobs


class Launcher:
    def __init__(self):
        self.requests = {}
        self.calls = 0
        self.lost = False
        self.state = 'running'

    def job_verify_prepare(self, job_id, request):
        self.calls += 1
        self.requests[job_id] = dict(request)
        if self.lost:
            raise OSError('response lost')

    def job_verify_result(self, job_id):
        return {'job_id': job_id, **self.requests[job_id], 'state': self.state}


def request():
    return {'project': 'example', 'target': 'target-1', 'revision': 'a' * 40,
            'run_id': 'run-1', 'attempt': 'head-1', 'recipe_id': 'recipe-1',
            'recipe_snapshot_id': 'snapshot-1', 'stage': 'verify', 'step': 'all'}


def test_lost_prepare_is_only_reconciled_after_restart(tmp_path):
    path = tmp_path / 'journal.db'
    journal = Journal(path, scrub=lambda value: value)
    launcher = Launcher()
    launcher.lost = True
    first = VerificationJobs(launcher, journal).poll(request())
    assert first['status'] == 'pending' and launcher.calls == 1
    journal.close()
    journal = Journal(path, scrub=lambda value: value)
    launcher.state = 'exited'
    result = VerificationJobs(launcher, journal).poll(request())
    assert result['status'] == 'exited' and result['job_id'] == first['job_id']
    assert launcher.calls == 1
    journal.close()


def test_scope_mismatch_and_unknown_never_claim_success(tmp_path):
    journal = Journal(tmp_path / 'journal.db', scrub=lambda value: value)
    launcher = Launcher()
    jobs = VerificationJobs(launcher, journal)
    result = jobs.poll(request())
    launcher.requests[result['job_id']]['step'] = 'different'
    with pytest.raises(DependencyUnavailable, match='identity_mismatch'):
        jobs.poll(request())
    launcher.requests[result['job_id']]['step'] = 'all'
    launcher.state = 'unknown'
    with pytest.raises(DependencyUnavailable, match='unconfirmed'):
        jobs.poll(request())
    with pytest.raises(DependencyUnavailable, match='binding_invalid'):
        jobs.poll({**request(), 'command': 'untrusted command'})
    assert launcher.calls == 1
    journal.close()


def test_host_runner_rejects_unconfirmed_execution():
    from plugins.subloop.change.integration.verification_runner import HostVerificationRunner
    from plugins.subloop.change.verification.policy import GateError
    class Jobs:
        receipt = {'status': 'pending', 'job_id': 'job-1'}
        def poll(self, binding):
            return self.receipt
    jobs = Jobs()
    runner = HostVerificationRunner(jobs)
    invocation = {'job_id': 'job-1', 'subject_sha': 'a' * 40, 'environment': 'verify-python',
                  'recipe_version': 'v1', 'command': 'verify', 'stage': 'verify', 'allow_hosts': [],
                  'launcher_request': request()}
    with pytest.raises(GateError, match='verification_pending'):
        runner.prepare(invocation)
    result = {'job_id': 'job-1', 'revision': 'a' * 40, 'runner': 'verify-python', 'recipe_version': 'v1',
              'command': 'verify', 'stage': 'verify', 'state': 'exited', 'isolation_verified': True,
              'executed': True, 'timed_out': False, 'oom_killed': False, 'log_complete': True,
              'output_truncated': False, 'exit_code': 0, 'log': 'ok', 'started_at': '2026-11-08T00:00:00Z',
              'ended_at': '2026-11-08T00:00:01Z', 'network_mode': 'none', 'allow_hosts': [],
              'home_absent': True, 'credentials_absent': True, 'host_config_absent': True, 'network_enforced': True}
    jobs.receipt = {'status': 'exited', 'job_id': 'job-1', 'result': result}
    prepared = runner.prepare(invocation)
    assert runner.execute(invocation, prepared)['output_tail'] == 'ok'
    for change in ({'isolation_verified': False}, {'log_complete': False}, {'timed_out': True},
                   {'command': 'other'}, {'network_mode': 'allowlist'}, {'oom_killed': True}, {'home_absent': False}):
        jobs.receipt = {'status': 'exited', 'job_id': 'job-1', 'result': {**result, **change}}
        with pytest.raises(GateError):
            runner.prepare(invocation)


def test_recipe_snapshot_persists_and_rejects_different_allowlist(tmp_path):
    from plugins.subloop.change.integration.verification_jobs import VerificationRuns
    class Pool:
        calls = []
        def post(self, path, body):
            self.calls.append((path, body))
            return {**body, 'request_id': path.rsplit('/', 1)[-1], 'recipe_snapshot_id': 'snapshot-1', 'recipe_version': 'v1', 'allow_hosts': ['packages.example'], 'prestart_isolation_enforced': True}
    path = tmp_path / 'journal.db'
    pool = Pool()
    journal = Journal(path, scrub=lambda value: value)
    runs = VerificationRuns(pool, journal)
    args = {'project': 'example', 'target': 'target-1', 'recipe_id': 'recipe-1', 'allowed_hosts': ['packages.example']}
    first = runs.begin({'item': 'issue-1', 'revision': 'rev-1'}, **args)
    journal.close()
    journal = Journal(path, scrub=lambda value: value)
    assert VerificationRuns(pool, journal).begin({'item': 'issue-1', 'revision': 'rev-1'}, **args) == first
    assert len(pool.calls) == 1
    with pytest.raises(DependencyUnavailable, match='allowlist_mismatch'):
        VerificationRuns(pool, journal).begin({'item': 'issue-2'}, **{**args, 'allowed_hosts': ['other.example']})
    journal.close()


def test_hosted_verification_resumes_same_job_and_binds_recipe(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import plugins.subloop.change.verification as verification
    from plugins.subloop.change.integration.verification_runner import HostedVerification
    recipe = {'version': 'v1', 'runner': 'verify-python', 'timeout_s': 60,
              'checks': [{'kind': 'all', 'command': 'verify'}]}
    monkeypatch.setattr(verification, 'load_recipes', lambda: {'recipe': recipe})
    class Pool:
        def __init__(self):
            self.jobs, self.posts, self.ready = {}, [], False
        def post(self, path, body):
            self.posts.append((path, body))
            if '/recipes/' in path:
                return {**body, 'request_id': path.rsplit('/', 1)[-1], 'recipe_snapshot_id': 'snapshot-1', 'recipe_version': 'v1', 'allow_hosts': [], 'prestart_isolation_enforced': True}
            job_id = path.rsplit('/', 1)[-1]
            self.jobs[job_id] = body.copy()
            return {'job_id': job_id, 'state': 'running'}
        def get(self, path):
            job_id = path.rsplit('/', 1)[-1]
            return {**self.jobs[job_id], 'job_id': job_id, 'state': 'exited' if self.ready else 'running',
                    'runner': 'verify-python', 'recipe_version': 'v1', 'command': 'verify',
                    'isolation_verified': True, 'executed': True, 'timed_out': False, 'oom_killed': False,
                    'log_complete': True, 'output_truncated': False, 'exit_code': 0, 'log': 'ok',
                    'started_at': '2026-11-08T00:00:00Z', 'ended_at': '2026-11-08T00:00:01Z',
                    'network_mode': 'none', 'allow_hosts': [], 'home_absent': True,
                    'credentials_absent': True, 'host_config_absent': True, 'network_enforced': True}
    target = SimpleNamespace(project='example', launcher_target='target-1', launcher_recipe_id='recipe-1',
                             implementation_repo='owner/code', test_recipe='recipe', trusted_authors=['trusted'],
                             untrusted_forbidden_paths=[], outbound_allowlist=[])
    registry = SimpleNamespace(resolve=lambda key: target)
    snapshot = {'repo': 'owner/code', 'pr': 1, 'head_sha': 'a' * 40, 'base_sha': 'b' * 40,
                'input_revision': 'rev-1', 'files_complete': True,
                'changed_files': [{'path': 'src/test.py', 'status': 'modified', 'binary': False}]}
    github = SimpleNamespace(view=lambda item, target: snapshot.copy())
    item = {'provider': 'github', 'issue_repo': 'owner/code', 'kind': 'pr', 'number': 1,
            'pr': 1, 'implementation_repo': 'owner/code', 'project': 'example', 'author': 'trusted'}
    configured = {'implementation_repo': 'owner/code'}
    path = tmp_path / 'journal.db'
    pool = Pool()
    journal = Journal(path, scrub=lambda value: value)
    port = HostedVerification(pool, journal, registry, github, scrub=lambda value: value)
    first = port.verify(item, configured, head_sha='a' * 40, base_sha='b' * 40)
    assert first['reason'] == 'verification_pending'
    assert len([x for x in pool.posts if '/jobs/' in x[0]]) == 1
    journal.close()
    journal = Journal(path, scrub=lambda value: value)
    pool.ready = True
    result = HostedVerification(pool, journal, registry, github, scrub=lambda value: value).verify(
        item, configured, head_sha='a' * 40, base_sha='b' * 40)
    assert result['status'] == 'passed'
    assert len([x for x in pool.posts if '/jobs/' in x[0]]) == 1
    assert len([x for x in pool.posts if '/recipes/' in x[0]]) == 1
    assert next(iter(pool.jobs.values()))['recipe_snapshot_id'] == 'snapshot-1'
    journal.close()


def test_recipe_without_prestart_isolation_is_never_started(tmp_path):
    from plugins.subloop.change.integration.verification_jobs import VerificationRuns
    class Pool:
        paths = []
        def post(self, path, body):
            self.paths.append(path)
            return {**body, 'request_id': path.rsplit('/', 1)[-1], 'recipe_snapshot_id': 'snapshot-1',
                    'recipe_version': 'v1', 'allow_hosts': []}
    journal = Journal(tmp_path / 'journal.db', scrub=lambda value: value)
    pool = Pool()
    with pytest.raises(DependencyUnavailable, match='prestart_isolation_unconfirmed'):
        VerificationRuns(pool, journal).begin({'item': 'one'}, project='example', target='target-1',
                                              recipe_id='recipe-1', allowed_hosts=[])
    assert all('/recipes/' in path for path in pool.paths)
    journal.close()


@pytest.mark.parametrize('scenario', ['lost-reply', 'recipe-changed', 'dependency',
    pytest.param('real-dependency', marks=pytest.mark.skipif(
        os.environ.get('AX_REAL_VERIFY_CANDIDATE') != 'fixed-proxy-ax-proxy-image-kjdacntd',
        reason='requires explicit isolated candidate'))])
def test_factory_through_pool_and_real_launcher_lost_reply(verify_system, tmp_path, monkeypatch, scenario):
    import importlib
    import json
    from types import SimpleNamespace
    from starlette.testclient import TestClient
    from axcommon.creds import sha256_hex
    import plugins.subloop.change.verification as verification
    from plugins.subloop.change.integration.factory import create_ports

    sb, base, head, _ = verify_system
    real = scenario == 'real-dependency'
    dependency = scenario in ('dependency', 'real-dependency')
    dep_command = 'true'
    verify_command = 'true'
    if real:
        sb.env['PATH'] = os.environ['PATH']
        sb.env['AX_ENV_SUDO'] = 'sudo -n'
        sb.env['AX_ENV_DOCKER'] = 'docker'
        (sb.state / 'deploy' / 'current.env').write_text('AX_TAG=fixed-proxy-ax-proxy-image-kjdacntd\n')
        dep_command = 'printf dependency-ok > packages/output; printf changed > source.txt'
        verify_command = 'test "$(cat packages/output)" = dependency-ok && test "$(cat source.txt)" = head'
    if dependency:
        import yaml
        recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
        recipes = yaml.safe_load(recipe_path.read_text())
        recipes['recipe-a'].update(dependency=[dep_command], dependency_artifacts=['packages'],
                                   outbound_allowlist=['packages.example.org'],
                                   checks=[{'kind': 'test', 'command': verify_command}])
        recipe_path.write_text(yaml.safe_dump(recipes))
    token_file = tmp_path / 'pool-tokens'
    token_file.write_text('change ' + sha256_hex('change-test') + '\n')
    monkeypatch.setenv('AX_PLUGIN_TOKENS', str(token_file))
    launcher_module = importlib.import_module('plugins.resource.worker-pool.launcher_client')
    commands = []
    def transport(command, stdin):
        commands.append(command)
        if scenario == 'recipe-changed' and 'job-verify-prepare' in command:
            import yaml
            recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
            recipes = yaml.safe_load(recipe_path.read_text())
            recipes['recipe-a']['version'] = 'v2'
            recipe_path.write_text(yaml.safe_dump(recipes))
        result = sb.subprocess_env_run(command.removeprefix('ax-env-run --caller launcher '),
                                       '--caller', 'launcher', stdin=stdin)
        return result.returncode, result.stdout
    launcher = launcher_module.LauncherClient(transport=transport)
    pool_module = importlib.import_module('plugins.resource.worker-pool.app')
    _, app, svc = pool_module.build(pool_module.Config(db=str(tmp_path / 'pool.sqlite')),
                                    launcher=launcher, run_heartbeat_loop=False, emit=lambda *a, **k: None)
    client = TestClient(app, headers={'Authorization': 'Bearer change-test'})
    class Pool:
        lost = False
        def post(self, path, body):
            response = client.post(path, json=body)
            assert response.status_code == 200, response.text
            if '/jobs/' in path and not self.lost:
                self.lost = True
                raise OSError('response lost after acceptance')
            return response.json()
        def get(self, path):
            response = client.get(path)
            assert response.status_code == 200, response.text
            return response.json()
    pool = Pool()
    target = {'issue_repo': 'owner/code', 'implementation_repo': 'owner/code', 'merge_method': 'merge',
              'ci_sources': [], 'test_recipe': 'recipe-a', 'priority_labels': [], 'execution_cap': 1,
              'allowed_paths': ['**'], 'untrusted_forbidden_paths': ['.github/**', 'package.json', 'Dockerfile*'], 'trusted_authors': ['trusted'],
              'outbound_allowlist': ['packages.example.org'] if dependency else [], 'project': 'project-a', 'launcher_target': 'target-a',
              'launcher_recipe_id': 'recipe-a'}
    targets = tmp_path / 'targets.json'
    targets.write_text(json.dumps({'targets': [target]}))
    monkeypatch.setenv('AX_CHANGE_TARGETS_FILE', str(targets))
    recipe = {'version': 'v1', 'runner': 'verify-python', 'timeout_s': 30,
              'checks': [{'kind': 'test', 'command': verify_command}]}
    if dependency:
        recipe['dependency'] = [dep_command]
    monkeypatch.setattr(verification, 'load_recipes', lambda: {'recipe-a': recipe})
    services = SimpleNamespace(plugin_dir=tmp_path, github=None, pool=pool,
                               ledger=SimpleNamespace(_token=None), control=None)
    item = {'provider': 'github', 'issue_repo': 'owner/code', 'kind': 'pr', 'number': 1,
            'pr': 1, 'implementation_repo': 'owner/code', 'project': 'project-a', 'author': 'trusted'}
    snapshot = {'head_sha': head, 'base_sha': base, 'input_revision': 'input-1', 'files_complete': True,
                'changed_files': [{'path': 'source.txt', 'status': 'modified', 'binary': False}]}
    path = tmp_path / 'journal.sqlite'
    journal = Journal(path, scrub=lambda value: value)
    ports = create_ports(services=services, journal=journal)
    ports.verification.github = SimpleNamespace(view=lambda *_: snapshot.copy())
    result = ports.verification.verify(item, target, head_sha=head, base_sha=base)
    if real:
        import time
        deadline = time.monotonic() + 45
        while result.get('reason') == 'verification_pending' and time.monotonic() < deadline:
            time.sleep(0.2)
            result = ports.verification.verify(item, target, head_sha=head, base_sha=base)
        assert result['status'] == 'passed', result
        assert len([c for c in commands if 'job-verify-prepare' in c]) == 2
        assert len([c for c in commands if 'job-verify-recipe ' in c]) == 1
        assert pool.lost
        journal.close()
        svc.store.close()
        return
    if scenario == 'recipe-changed':
        assert result['status'] == 'parked' and result['reason'] != 'verification_pending', result
        assert not any(call['argv'][0] in ('create', 'start') for call in sb.fd.calls())
        journal.close()
        svc.store.close()
        return
    assert result['reason'] == 'verification_pending', result
    prepare_calls = [c for c in commands if 'job-verify-prepare' in c]
    assert len(prepare_calls) == 1 and pool.lost
    job_id = prepare_calls[0].split()[-1]
    if dependency:
        from pathlib import Path
        dep = sb.fd.container('ax-verify-' + job_id)
        mount = next(m for m in dep['Mounts'] if m['Destination'] == '/work/repo/packages')
        assert mount['RW'] is True
        Path(mount['Source'], 'artifact.txt').write_text('dependency output')
    sb.fd.finish('ax-verify-' + job_id, exit_code=0, logs='ok\n')
    journal.close()
    journal = Journal(path, scrub=lambda value: value)
    ports = create_ports(services=services, journal=journal)
    ports.verification.github = SimpleNamespace(view=lambda *_: snapshot.copy())
    result = ports.verification.verify(item, target, head_sha=head, base_sha=base)
    if dependency:
        assert result['reason'] == 'verification_pending', result
        prepare_calls = [c for c in commands if 'job-verify-prepare' in c]
        assert len(prepare_calls) == 2
        verify_id = prepare_calls[-1].split()[-1]
        check = sb.fd.container('ax-verify-' + verify_id)
        assert check['HostConfig']['NetworkMode'] == 'none'
        mount = next(m for m in check['Mounts'] if m['Destination'] == '/work/repo/packages')
        assert mount['RW'] is False and Path(mount['Source'], 'artifact.txt').read_text() == 'dependency output'
        sb.fd.finish('ax-verify-' + verify_id, exit_code=0, logs='ok\n')
        result = ports.verification.verify(item, target, head_sha=head, base_sha=base)
    assert result['status'] == 'passed', result
    assert len([c for c in commands if 'job-verify-prepare' in c]) == (2 if dependency else 1)
    assert len([c for c in commands if 'job-verify-recipe ' in c]) == 1
    journal.close()
    svc.store.close()


from tests.b6b.conftest import sb
from tests.b6b.test_verify_launcher_e2e import verify_system

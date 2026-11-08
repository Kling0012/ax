"""Exercise verification through the forced launcher command in an isolated sandbox."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.b6b.helpers import FAKE_DOCKER


def invoke(sb, command, body=None, extra_env=None):
    result = sb.subprocess_env_run(command, '--caller', 'launcher',
        stdin=json.dumps(body or {}), extra_env=extra_env)
    return result.returncode, json.loads(result.stdout)


@pytest.fixture
def verify_system(sb):
    repo = sb.tmp / 'source-repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (repo / 'source.txt').write_text('base\n')
    git('add', 'source.txt')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (repo / 'source.txt').write_text('head\n')
    git('commit', '-qam', 'head')
    head = git('rev-parse', 'HEAD')
    (sb.ax_home / 'config' / 'ro-mounts.yaml').write_text(yaml.safe_dump([
        {'id': 'target-a', 'project': 'project-a', 'host_path': str(repo)}]))
    (sb.ax_home / 'config' / 'verification-recipes.yaml').write_text(yaml.safe_dump({
        'recipe-a': {'project': 'project-a', 'target': 'target-a', 'version': 'v1',
                     'runner': 'verify-python', 'timeout_s': 30,
                     'checks': [{'kind': 'test', 'command': 'true'}]}}))
    bindir = sb.tmp / 'bin'
    bindir.mkdir()
    timer = bindir / 'systemd-run'
    timer.write_text('#!/bin/sh\nexit 0\n')
    timer.chmod(0o755)
    systemctl = bindir / 'systemctl'
    systemctl.write_text('#!/bin/sh\nif [ "$FAKE_TIMER_MISSING" = 1 ]; then exit 3; fi\nprintf "active\\n"\n')
    systemctl.chmod(0o755)
    docker = bindir / 'docker-wrapper.py'
    docker.write_text('''import json, os, subprocess, sys
args=sys.argv[1:]
if args and args[0]=='logs' and os.environ.get('FAKE_LOG_MISSING'):
    sys.exit(1)
if args and args[0] in ('run', 'create'):
    for flag in ('--log-driver','--log-opt'):
        while flag in args:
            i=args.index(flag); args=args[:i]+args[i+2:]
r=subprocess.run([sys.executable, FAKE_DOCKER, *args], capture_output=True, text=True)
if args and args[0]=='inspect' and r.returncode==0:
    data=json.loads(r.stdout)
    for item in data:
        item['HostConfig']['LogConfig']={'Type':'json-file','Config':{'max-size':'-1','mode':'blocking'}}
        item['Config']['User']=item.get('User')
        item['State'].setdefault('OOMKilled',False)
    sys.stdout.write(json.dumps(data))
else:
    sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
'''.replace('FAKE_DOCKER', repr(str(FAKE_DOCKER))))
    sb.env['PATH'] = str(bindir) + os.pathsep + sb.env['PATH']
    sb.env['AX_ENV_DOCKER'] = sys.executable + ' ' + str(docker)
    snapshots = {}
    def request(revision=head, run_id='run-a', attempt='head-1'):
        if run_id not in snapshots:
            code, recipe = invoke(sb, 'job-verify-recipe recipe-' + str(len(snapshots)), {'project': 'project-a', 'target': 'target-a', 'recipe_id': 'recipe-a'})
            assert code == 0, recipe
            snapshots[run_id] = recipe['recipe_snapshot_id']
        return {'recipe_snapshot_id': snapshots[run_id], 'project': 'project-a', 'target': 'target-a', 'revision': revision,
                'run_id': run_id, 'attempt': attempt, 'recipe_id': 'recipe-a',
                'stage': 'verify', 'step': 'test'}
    return sb, base, head, request


def test_fixed_revision_results_and_independent_jobs(verify_system):
    sb, base, head, req = verify_system
    for jid, request in [('base-job', req(base, attempt='base')),
                         ('head-job', req(head)), ('retry-job', req(head, attempt='head-2'))]:
        code, out = invoke(sb, 'job-verify-prepare ' + jid, request)
        assert code == 0, out
        assert out['state'] == 'running'
        sb.fd.finish('ax-verify-' + jid, exit_code=0, logs='all output\n')
        code, result = invoke(sb, 'job-verify-result ' + jid)
        assert code == 0 and result['revision'] == request['revision']
        assert result['state'] == 'exited' and result['exit_code'] == 0
        assert result['network_mode'] == 'none' and result['isolation_verified'] is True
        assert all(result[k] is True for k in ('home_absent', 'credentials_absent', 'host_config_absent', 'network_enforced'))
        assert result['log_complete'] is True and result['log'] == 'all output\n'
        assert invoke(sb, 'job-verify-result ' + jid)[1] == result
    assert sb.fd.container('ax-verify-base-job') is not None


def test_network_mismatch_rejects_and_missing_log_is_incomplete(verify_system):
    sb, _, _, req = verify_system
    code, out = invoke(sb, 'job-verify-prepare bad-net', req(), {'FAKE_DOCKER_BREAK': 'network'})
    assert code != 0 and out['error']['reason'] == 'isolation_unconfirmed'
    calls = [item['argv'] for item in sb.fd.calls()]
    assert any(argv[0] == 'create' for argv in calls)
    assert not any(argv[0] == 'start' for argv in calls)
    code, out = invoke(sb, 'job-verify-prepare log-job', req(attempt='head-2'))
    assert code == 0, out
    sb.fd.finish('ax-verify-log-job', exit_code=0, logs='output')
    code, result = invoke(sb, 'job-verify-result log-job', extra_env={'FAKE_LOG_MISSING': '1'})
    assert code == 0 and result['log_complete'] is False and result['output_truncated'] is True


def test_timeout_and_missing_monitor_never_succeed(verify_system):
    sb, _, _, req = verify_system
    assert invoke(sb, 'job-verify-prepare timeout-job', req())[0] == 0
    assert invoke(sb, 'job-verify-expire timeout-job')[0] == 0
    result = invoke(sb, 'job-verify-result timeout-job')[1]
    assert result['timed_out'] is True
    assert invoke(sb, 'job-verify-prepare other-job', req(attempt='head-2'))[0] == 0
    sb.fd.finish('ax-verify-other-job', exit_code=0, logs='ok')
    result = invoke(sb, 'job-verify-result other-job', extra_env={'FAKE_TIMER_MISSING': '1'})[1]
    assert result['state'] == 'unknown' and result['log_complete'] is False


def test_changed_repo_is_not_reused_by_next_job(verify_system):
    sb, _, _, req = verify_system
    assert invoke(sb, 'job-verify-prepare job-one', req())[0] == 0
    container = sb.fd.container('ax-verify-job-one')
    first = next(m['Source'] for m in container['Mounts'] if m['Destination'] == '/work/repo')
    Path(first, 'source.txt').write_text('modified by job\n')
    sb.fd.finish('ax-verify-job-one', exit_code=0, logs='ok')
    assert invoke(sb, 'job-verify-result job-one')[1]['state'] == 'exited'
    assert invoke(sb, 'job-verify-prepare job-two', req())[0] == 0
    container = sb.fd.container('ax-verify-job-two')
    second = next(m['Source'] for m in container['Mounts'] if m['Destination'] == '/work/repo')
    assert second != first and Path(second, 'source.txt').read_text() == 'head\n'


def test_oom_and_invalid_request_are_not_success(verify_system):
    sb, _, _, req = verify_system
    code, out = invoke(sb, 'job-verify-prepare invalid-job', {**req(), 'command': 'true'})
    assert code != 0 and out['error']['reason'] == 'bad_field'
    assert invoke(sb, 'job-verify-prepare oom-job', req())[0] == 0
    sb.fd.finish('ax-verify-oom-job', exit_code=137, logs='killed')
    state = sb.fd.load()
    state['containers']['ax-verify-oom-job']['State']['OOMKilled'] = True
    sb.fd.save(state)
    code, result = invoke(sb, 'job-verify-result oom-job')
    assert code == 0 and result['oom_killed'] is True and result['exit_code'] == 137


def test_saved_log_tampering_is_detected_and_monitor_loss_stops(verify_system):
    sb, _, _, req = verify_system
    assert invoke(sb, 'job-verify-prepare saved-job', req())[0] == 0
    sb.fd.finish('ax-verify-saved-job', exit_code=0, logs='original')
    assert invoke(sb, 'job-verify-result saved-job')[1]['log_complete'] is True
    (sb.state / 'verification-jobs' / 'saved-job.log').write_text('modified')
    result = invoke(sb, 'job-verify-result saved-job')[1]
    assert result['log_complete'] is False and result['output_truncated'] is True
    assert invoke(sb, 'job-verify-prepare running-job', req(attempt='head-2'))[0] == 0
    result = invoke(sb, 'job-verify-result running-job', extra_env={'FAKE_TIMER_MISSING': '1'})[1]
    assert result['state'] == 'unknown' and result['log_complete'] is False
    assert sb.fd.container('ax-verify-running-job')['State']['Status'] == 'exited'
    assert invoke(sb, 'job-verify-result running-job')[1]['state'] == 'unknown'


def test_registered_variants_and_scope_are_enforced(verify_system):
    sb, _, _, req = verify_system
    recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = yaml.safe_load(recipe_path.read_text())
    recipes['recipe-a']['all'] = {'kind': 'all', 'command': 'true'}
    recipes['recipe-a']['slow'] = {'kind': 'slow', 'command': 'true'}
    recipe_path.write_text(yaml.safe_dump(recipes))
    for step in ('all', 'slow'):
        request = {**req(run_id='run-' + step), 'step': step}
        code, result = invoke(sb, 'job-verify-prepare job-' + step, request)
        assert code == 0, result
    code, result = invoke(sb, 'job-verify-prepare unregistered-job', {**req(run_id='run-unknown'), 'step': 'unknown'})
    assert code != 0 and result['error']['reason'] == 'unknown_step'
    code, result = invoke(sb, 'job-verify-prepare changed-job', {**req(run_id='run-all'), 'target': 'other'})
    assert code != 0


def test_real_user_timer_stops_fake_job(verify_system):
    import shlex
    import time
    from tests.b6b.helpers import FAKE_SUDO
    sb, _, _, req = verify_system
    if not Path('/run/user', str(os.getuid()), 'bus').exists():
        pytest.skip('user systemd bus unavailable')
    env = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}',
               DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
    check = subprocess.run(['systemctl', '--user', 'show-environment'], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode:
        pytest.skip('user systemd bus unavailable')
    wrapper = sb.tmp / 'bin' / 'docker-wrapper.py'
    sudo_wrapper = sb.tmp / 'sudo-wrapper'
    sudo_wrapper.write_text('#!/bin/sh\nexport FAKE_SUDO_LOG=' + shlex.quote(str(sb.sudo_log))
        + '\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(FAKE_SUDO)) + ' "$@"\n')
    sudo_wrapper.chmod(0o755)
    docker_wrapper = sb.tmp / 'docker-wrapper'
    docker_wrapper.write_text('#!/bin/sh\nexport FAKE_DOCKER_DIR=' + shlex.quote(str(sb.fd.root))
        + '\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(wrapper)) + ' "$@"\n')
    docker_wrapper.chmod(0o755)
    sb.env['PATH'] = os.environ['PATH']
    sb.env['AX_ENV_SUDO'] = str(sudo_wrapper) + ' -n'
    sb.env['AX_ENV_DOCKER'] = str(docker_wrapper)
    recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = yaml.safe_load(recipe_path.read_text())
    recipes['recipe-a']['timeout_s'] = 4
    recipe_path.write_text(yaml.safe_dump(recipes))
    import uuid
    job = 'timer-' + uuid.uuid4().hex[:12]
    code, result = invoke(sb, 'job-verify-prepare ' + job, req(run_id='run-timer'))
    assert code == 0, result
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record_path = sb.state / 'verification-jobs' / (job + '.json')
        if record_path.exists() and json.loads(record_path.read_text()).get('timed_out'):
            break
        time.sleep(.2)
    code, result = invoke(sb, 'job-verify-result ' + job)
    assert code == 0 and result.get('timed_out') is True
    assert sb.fd.container('ax-verify-' + job)['State']['Status'] == 'exited'


def test_exit_inspection_mismatch_is_not_success(verify_system):
    sb, _, _, req = verify_system
    assert invoke(sb, 'job-verify-prepare changed-exit', req())[0] == 0
    sb.fd.finish('ax-verify-changed-exit', exit_code=0, logs='ok')
    state = sb.fd.load()
    state['containers']['ax-verify-changed-exit']['HostConfig']['NetworkMode'] = 'bridge'
    sb.fd.save(state)
    code, result = invoke(sb, 'job-verify-result changed-exit')
    assert code == 0 and result['state'] == 'unknown'
    assert result['isolation_verified'] is False and result['log_complete'] is False


def test_recipe_snapshot_is_idempotent_and_change_blocks_start(verify_system):
    sb, _, _, req = verify_system
    body = {'project': 'project-a', 'target': 'target-a', 'recipe_id': 'recipe-a'}
    code, first = invoke(sb, 'job-verify-recipe stable-request', body)
    assert code == 0
    assert invoke(sb, 'job-verify-recipe stable-request', body) == (0, first)
    code, changed_scope = invoke(sb, 'job-verify-recipe stable-request', {**body, 'project': 'other'})
    assert code != 0 and changed_scope['error']['reason'] == 'recipe_request_mismatch'
    request = {**req(run_id='run-snapshot'), 'recipe_snapshot_id': first['recipe_snapshot_id']}
    recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = yaml.safe_load(recipe_path.read_text())
    recipes['recipe-a']['checks'][0]['command'] = 'false'
    recipe_path.write_text(yaml.safe_dump(recipes))
    code, result = invoke(sb, 'job-verify-prepare stale-recipe', request)
    assert code != 0 and result['error']['reason'] == 'recipe_snapshot_mismatch'
    assert sb.fd.container('ax-verify-stale-recipe') is None


def test_recipe_snapshot_concurrent_retries_return_same_snapshot(verify_system):
    from concurrent.futures import ThreadPoolExecutor
    sb, _, _, _ = verify_system
    body = {'project': 'project-a', 'target': 'target-a', 'recipe_id': 'recipe-a'}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: invoke(sb, 'job-verify-recipe concurrent-request', body), range(4)))
    assert all(code == 0 and result == results[0][1] for code, result in results)
    assert invoke(sb, 'job-verify-recipe concurrent-request', body) == results[0]


def test_dependency_artifacts_are_scoped_and_verification_is_offline(verify_system):
    sb, _, _, req = verify_system
    recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = yaml.safe_load(recipe_path.read_text())
    recipes['recipe-a'].update(dependency=['true'], dependency_artifacts=['packages'], outbound_allowlist=['packages.example.org'])
    recipe_path.write_text(yaml.safe_dump(recipes))
    dep_request = {**req(run_id='run-dependency'), 'stage': 'dependency', 'step': 'dependency-1'}
    assert invoke(sb, 'job-verify-prepare premature', {**dep_request, 'stage': 'verify', 'step': 'test'})[0] != 0
    code, result = invoke(sb, 'job-verify-prepare dep-job', dep_request)
    assert code == 0, result
    dep = sb.fd.container('ax-verify-dep-job')
    assert dep['HostConfig']['NetworkMode'] == 'ax-vj-dep-job' and dep['HostConfig']['Dns'] == ['127.0.0.1']
    mount = next(m for m in dep['Mounts'] if m['Destination'] == '/work/repo/packages')
    Path(mount['Source'], 'artifact.txt').write_text('dependency output')
    source_repo = next(m['Source'] for m in dep['Mounts'] if m['Destination'] == '/work/repo')
    Path(source_repo, 'source.txt').write_text('modified by dependency')
    sb.fd.finish('ax-verify-dep-job', exit_code=0, logs='ok')
    code, result = invoke(sb, 'job-verify-result dep-job')
    assert code == 0 and result['state'] == 'exited' and result['network_enforced'] is True
    assert sb.fd.container('ax-vp-dep-job')['State']['Status'] == 'exited'
    code, result = invoke(sb, 'job-verify-prepare check-job', {**dep_request, 'stage': 'verify', 'step': 'test'})
    assert code == 0, result
    check = sb.fd.container('ax-verify-check-job')
    assert check['HostConfig']['NetworkMode'] == 'none'
    check_repo = next(m['Source'] for m in check['Mounts'] if m['Destination'] == '/work/repo')
    assert Path(check_repo, 'source.txt').read_text() == 'head\n'
    check_mount = next(m for m in check['Mounts'] if m['Destination'] == '/work/repo/packages')
    assert check_mount['RW'] is False and Path(check_mount['Source'], 'artifact.txt').read_text() == 'dependency output'
    other = {**req(run_id='run-other'), 'stage': 'verify', 'step': 'test'}
    assert invoke(sb, 'job-verify-prepare other-run', other)[0] != 0


def test_dependency_failure_and_artifact_source_overlap_are_rejected(verify_system):
    sb, _, _, req = verify_system
    recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = yaml.safe_load(recipe_path.read_text())
    recipes['recipe-a'].update(dependency=['true'], dependency_artifacts=['source.txt'], outbound_allowlist=['packages.example.org'])
    recipe_path.write_text(yaml.safe_dump(recipes))
    dep = {**req(run_id='overlap-run'), 'stage': 'dependency', 'step': 'dependency-1'}
    code, result = invoke(sb, 'job-verify-prepare overlap-job', dep)
    assert code != 0 and result['error']['reason'] == 'artifact_overlaps_source'
    assert sb.fd.container('ax-verify-overlap-job') is None
    recipes['recipe-a']['dependency_artifacts'] = ['packages']
    recipe_path.write_text(yaml.safe_dump(recipes))
    dep = {**req(run_id='failed-run'), 'stage': 'dependency', 'step': 'dependency-1'}
    assert invoke(sb, 'job-verify-prepare failed-dep', dep)[0] == 0
    sb.fd.finish('ax-verify-failed-dep', exit_code=1, logs='failure')
    assert invoke(sb, 'job-verify-result failed-dep')[1]['exit_code'] == 1
    code, result = invoke(sb, 'job-verify-prepare after-failure', {**dep, 'stage': 'verify', 'step': 'test'})
    assert code != 0 and result['error']['reason'] == 'dependency_not_completed'
    assert sb.fd.container('ax-verify-after-failure') is None
    assert not any(call['argv'][0] in ('rm', 'prune') for call in sb.fd.calls())


def test_dependency_proxy_mismatch_never_starts_job(verify_system):
    sb, _, _, req = verify_system
    recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = yaml.safe_load(recipe_path.read_text())
    recipes['recipe-a'].update(dependency=['true'], dependency_artifacts=['packages'], outbound_allowlist=['packages.example.org'])
    recipe_path.write_text(yaml.safe_dump(recipes))
    dep = {**req(run_id='bad-proxy-run'), 'stage': 'dependency', 'step': 'dependency-1'}
    code, _ = invoke(sb, 'job-verify-prepare bad-proxy', dep, {'FAKE_DOCKER_BREAK': 'network'})
    assert code != 0
    assert not any(call['argv'][:2] in (['start', 'ax-vp-bad-proxy'], ['start', 'ax-verify-bad-proxy']) for call in sb.fd.calls())
    assert sb.fd.container('ax-verify-bad-proxy') is None


def test_dependency_timeout_stops_job_and_proxy_without_success(verify_system):
    sb, _, _, req = verify_system
    recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = yaml.safe_load(recipe_path.read_text())
    recipes['recipe-a'].update(dependency=['true'], dependency_artifacts=['packages'], outbound_allowlist=['packages.example.org'])
    recipe_path.write_text(yaml.safe_dump(recipes))
    dep = {**req(run_id='timeout-dependency'), 'stage': 'dependency', 'step': 'dependency-1'}
    assert invoke(sb, 'job-verify-prepare timed-dep', dep)[0] == 0
    assert invoke(sb, 'job-verify-expire timed-dep')[0] == 0
    result = invoke(sb, 'job-verify-result timed-dep')[1]
    assert result['timed_out'] is True
    assert sb.fd.container('ax-verify-timed-dep')['State']['Status'] == 'exited'
    assert sb.fd.container('ax-vp-timed-dep')['State']['Status'] == 'exited'
    code, result = invoke(sb, 'job-verify-prepare timed-check', {**dep, 'stage': 'verify', 'step': 'test'})
    assert code != 0 and result['error']['reason'] == 'dependency_not_completed'


def test_dependency_proxy_tampering_and_monitor_loss_fail_closed(verify_system):
    sb, _, _, req = verify_system
    recipe_path = sb.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = yaml.safe_load(recipe_path.read_text())
    recipes['recipe-a'].update(dependency=['true'], dependency_artifacts=['packages'], outbound_allowlist=['packages.example.org'])
    recipe_path.write_text(yaml.safe_dump(recipes))
    dep = {**req(run_id='proxy-tamper'), 'stage': 'dependency', 'step': 'dependency-1'}
    assert invoke(sb, 'job-verify-prepare tamper-dep', dep)[0] == 0
    state = sb.fd.load()
    state['containers']['ax-vp-tamper-dep']['HostConfig']['Sysctls']['net.ipv4.ip_forward'] = '1'
    sb.fd.save(state)
    code, result = invoke(sb, 'job-verify-result tamper-dep')
    assert code == 0 and result['state'] == 'unknown' and result['network_enforced'] is False
    assert sb.fd.container('ax-vp-tamper-dep')['State']['Status'] == 'exited'
    assert sb.fd.container('ax-verify-tamper-dep')['State']['Status'] == 'exited'
    dep = {**req(run_id='lost-monitor'), 'stage': 'dependency', 'step': 'dependency-1'}
    assert invoke(sb, 'job-verify-prepare lost-dep', dep)[0] == 0
    code, result = invoke(sb, 'job-verify-result lost-dep', extra_env={'FAKE_TIMER_MISSING': '1'})
    assert code == 0 and result['state'] == 'unknown'
    assert sb.fd.container('ax-vp-lost-dep')['State']['Status'] == 'exited'
    assert sb.fd.container('ax-verify-lost-dep')['State']['Status'] == 'exited'
    assert invoke(sb, 'job-verify-result lost-dep')[1]['state'] == 'unknown'

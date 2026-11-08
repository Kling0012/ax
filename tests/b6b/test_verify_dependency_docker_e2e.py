"""Opt-in synthetic verification on real Docker; resources remain stopped."""
import os
import time
import uuid

import pytest
import yaml

from tests.b6b.test_verify_launcher_e2e import invoke, verify_system


@pytest.mark.skipif(not os.environ.get('AX_REAL_VERIFY_CANDIDATE'), reason='set AX_REAL_VERIFY_CANDIDATE to an existing local image tag')
def test_real_dependency_then_offline_verify(verify_system):
    sb, _, _, req = verify_system
    sb.env['PATH'] = os.environ['PATH']
    sb.env['AX_ENV_SUDO'] = 'sudo -n'
    sb.env['AX_ENV_DOCKER'] = 'docker'
    (sb.state / 'deploy' / 'current.env').write_text('AX_TAG=' + os.environ['AX_REAL_VERIFY_CANDIDATE'] + '\n')
    recipe_file = sb.ax_home / 'config' / 'verification-recipes.yaml'
    recipes = yaml.safe_load(recipe_file.read_text())
    recipes['recipe-a'].update(
        timeout_s=40, outbound_allowlist=['packages.example.invalid'], dependency_artifacts=['packages'],
        dependency=['printf dependency-ok > packages/output; printf changed > source.txt'],
        checks=[{'kind': 'test', 'command': 'test "$(cat packages/output)" = dependency-ok && test "$(cat source.txt)" = head'}])
    recipe_file.write_text(yaml.safe_dump(recipes))
    suffix = uuid.uuid4().hex[:10]
    request = req(run_id='real-' + suffix)
    dependency = {**request, 'stage': 'dependency', 'step': 'dependency-1'}
    def execute(job_id, body):
        code, data = invoke(sb, 'job-verify-prepare ' + job_id, body)
        assert code == 0, data
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            code, data = invoke(sb, 'job-verify-result ' + job_id)
            assert code == 0, data
            if data['state'] not in ('starting', 'running'):
                return data
            time.sleep(.2)
        pytest.fail('job did not complete')
    dep = execute('dep-' + suffix, dependency)
    assert dep['state'] == 'exited' and dep['exit_code'] == 0 and dep['network_mode'] == 'allowlist'
    check = execute('check-' + suffix, {**request, 'stage': 'verify', 'step': 'test'})
    assert check['state'] == 'exited' and check['exit_code'] == 0 and check['network_mode'] == 'none'
    assert all(check[k] is True for k in ('isolation_verified', 'home_absent', 'credentials_absent', 'host_config_absent', 'network_enforced'))

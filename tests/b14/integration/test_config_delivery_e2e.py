"""Exercise deployment config using fake files and real configuration loaders."""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from core.resources.envs.registry import load_environment_file
from plugins.subloop.change.integration.runtime import build_executor
from runtime.subloop.config import build_services
from tests.fakes.b14.integration import target
from tests.fakes.b7 import make_env


def compose(root, extra=(), env=None, env_file=None):
    files = [root / 'compose.yaml', *sorted((root / 'compose.d').glob('*.yaml')), *extra]
    cmd = ['docker', 'compose', '-p', 'ax-config-test', '--project-directory', str(root),
           '--env-file', str(root / 'tools/ci-fixtures/ci.env')]
    if env_file is not None:
        cmd += ['--env-file', str(env_file)]
    for file in files:
        cmd += ['-f', str(file)]
    cmd += ['--profile', '*', 'config', '--format', 'json']
    clean = {k: v for k, v in os.environ.items() if not k.startswith('AX_')}
    clean.update(env or {})
    result = subprocess.run(cmd, env=clean, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)['services']


def test_compose_defaults_and_publisher_value(repo_root):
    services = compose(repo_root)
    assert services['resources']['environment']['AX_ENVIRONMENTS_FILE'] == '/ax/config/environments.yaml'
    assert services['sl-release']['environment']['AX_PRODUCTS_FILE'] == '/ax/config/products.yaml'
    assert services['sl-change']['environment']['AX_CHANGE_TARGETS_FILE'] == '/ax/plugins/subloop/change/targets.yaml'
    assert services['sl-scheduled-publisher']['environment']['AX_SCHEDULED_PUBLISHER_URL_PREFIXES'].startswith('https://github.com/example-owner/')
    services = compose(repo_root, env={'AX_SCHEDULED_PUBLISHER_URL_PREFIXES': 'https://fixture.invalid/allowed/'})
    assert services['sl-scheduled-publisher']['environment']['AX_SCHEDULED_PUBLISHER_URL_PREFIXES'] == 'https://fixture.invalid/allowed/'
    for service in ('resources', 'sl-release', 'sl-change'):
        assert not any(v.get('target', '').startswith('/run/ax-config/') for v in services[service].get('volumes', []))


def test_fake_bind_contract_and_real_loaders(tmp_path, repo_root, monkeypatch, harness, fake_clock):
    env_data = yaml.safe_load((repo_root / 'config/environments.example.yaml').read_text())
    row = env_data['environments'][0]
    row['id'] = 'fixture-env'
    env_data['environments'] = [row]
    env_file = tmp_path / 'environments.yaml'
    env_file.write_text(yaml.safe_dump(env_data))
    products = yaml.safe_load((repo_root / 'config/products.yaml').read_text())
    products['products'][0]['id'] = 'fixture-product'
    product_file = tmp_path / 'products.yaml'
    product_file.write_text(yaml.safe_dump(products))
    targets_file = tmp_path / 'targets.yaml'
    targets_file.write_text(json.dumps({'targets': [target()]}))
    mappings = [('resources', 'AX_ENVIRONMENTS_FILE', env_file, '/run/ax-config/environments.yaml'),
                ('sl-release', 'AX_PRODUCTS_FILE', product_file, '/run/ax-config/products.yaml'),
                ('sl-change', 'AX_CHANGE_TARGETS_FILE', targets_file, '/run/ax-config/change-targets.yaml')]
    overlay = {'services': {service: {'environment': {key: dest}, 'volumes': [
        {'type': 'bind', 'source': str(source), 'target': dest, 'read_only': True}]}
        for service, key, source, dest in mappings}}
    fragment = tmp_path / 'override.yaml'
    fragment.write_text(yaml.safe_dump(overlay))
    services = compose(repo_root, [fragment])
    for service, key, source, dest in mappings:
        assert services[service]['environment'][key] == dest
        assert any(v.get('source') == str(source) and v.get('target') == dest and v.get('read_only')
                   for v in services[service]['volumes'])
    monkeypatch.setenv('AX_ENVIRONMENTS_FILE', str(env_file))
    registry = load_environment_file()
    assert registry.get('fixture-env') is not None and not registry.rejected
    release = build_services({'AX_PLUGIN_ID': 'release', 'AX_DATA_DIR': str(tmp_path / 'release'),
                              'AX_PRODUCTS_FILE': str(product_file)}, repo_root=repo_root)
    assert release.release_service.registry.get('fixture-product')['dry_run'] is True
    (tmp_path / 'fake').mkdir()
    fake = make_env(tmp_path / 'fake', repo_root, fake_clock)
    ex, flow, errors = build_executor(root=repo_root, services=fake.svc, ports=harness.ports,
        environ={'AX_DATA_DIR': str(tmp_path / 'change'), 'AX_CHANGE_TARGETS_FILE': str(targets_file)})
    try:
        assert not errors and ex.mode == 'collect_only'
        assert set(flow.targets) == {target()['issue_repo']}
        assert not harness.work.calls and not harness.github.merges
    finally:
        ex.store.close()
        ex.change_journal.close()
        fake.store.close()


def test_missing_change_adapters_stay_local(tmp_path, repo_root, fake_clock):
    fake = make_env(tmp_path, repo_root, fake_clock)
    ex, flow, errors = build_executor(root=repo_root, services=fake.svc,
        environ={'AX_DATA_DIR': str(tmp_path / 'change')})
    try:
        assert flow is None and ex.mode == 'collect_only' and ex.graph.entry == 'none'
        assert any(e['code'] == 'change_ports_unconfigured' for e in errors)
    finally:
        ex.store.close()
        ex.change_journal.close()
        fake.store.close()


def test_publisher_launcher_value_reaches_url_check(tmp_path, repo_root):
    launcher = tmp_path / 'launcher.env'
    launcher.write_text('AX_SCHEDULED_PUBLISHER_URL_PREFIXES=https://fixture.invalid/allowed/\n')
    services = compose(repo_root, env_file=launcher)
    value = services['sl-scheduled-publisher']['environment']['AX_SCHEDULED_PUBLISHER_URL_PREFIXES']
    code = """
from plugins.timer.scheduled_publisher.checks import OutboxCheck
from plugins.timer.scheduled_publisher.model import Candidate, Settings
class Redactor:
    index_ok = pii_list_ok = True
    def contains_secret(self, value): return False
check = OutboxCheck(Redactor(), lambda candidate: True, Settings())
assert check.shape(Candidate(action='publish', text='https://fixture.invalid/allowed/item')) is None
assert check.shape(Candidate(action='publish', text='https://example.invalid/')) == 'url_not_allowed'
"""
    subprocess.run([sys.executable, '-c', code], cwd=repo_root, check=True,
                   env={**os.environ, 'AX_SCHEDULED_PUBLISHER_URL_PREFIXES': value,
                        'PYTHONDONTWRITEBYTECODE': '1'})

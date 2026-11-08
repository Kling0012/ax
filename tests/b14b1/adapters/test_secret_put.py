import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.resources.envs.adapters.base import AdapterError
from core.resources.envs.adapters.ssh_host import SSHHostAdapter

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('ax_launcher_secret_test', ROOT / 'host/env-module-min/releases/b6/lib/ax_launcher.py')
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def make_environment(tmp_path, *, runner):
    return SSHHostAdapter.from_environment({
        'id': 'compute-host', 'adapter': 'ssh-host', 'state': 'available',
        'connect': {'kind': 'ssh', 'host': 'host.example', 'user': 'user', 'port': 22},
        'conn_credential': {'slot': 'env-ssh-compute-host', 'place': 'secrets_dir/env-ssh-compute-host'},
        'secret_targets': [{'name': 'token', 'max_bytes': 32}],
    }, secrets_dir=str(tmp_path), runner=runner)


def test_secret_transport_and_allowlist(tmp_path):
    calls = []
    secret = b'confidential\x00bytes'
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'target': 'token', 'size': len(secret), 'mtime': '20261108T120000Z', 'mode': '600', 'archived': None}).encode())
    adapter = make_environment(tmp_path, runner=runner)
    with pytest.raises(AdapterError):
        adapter.secret_put('other', secret)
    assert not calls
    assert adapter.secret_put('token', secret)['size'] == len(secret)
    argv, args = calls[0]
    assert secret not in repr(argv).encode()
    assert args['input'] == secret
    assert argv[-2:] == ['secret-put', 'token']


def test_secret_transport_sanitizes_remote_errors(tmp_path):
    def runner(argv, **kwargs):
        raise RuntimeError('secret-value')
    adapter = make_environment(tmp_path, runner=runner)
    with pytest.raises(AdapterError) as exc:
        adapter.secret_put('token', b'secret-value')
    assert 'secret-value' not in str(exc.value)


def setup_manifest(tmp_path):
    home = tmp_path / 'env'
    home.mkdir()
    target = tmp_path / 'target'
    (home / 'manifest.yaml').write_text(yaml.safe_dump({
        'secret_targets': [{'name': 'token', 'path': str(target), 'mode': '600', 'max_bytes': 8}],
        'constraints': {'stop_files': ['PAUSE']},
    }))
    return home, target


def run_put(home, value, name='token', caller='env'):
    source = io.BytesIO(value)
    return launcher.dispatch(['--caller', caller, 'secret-put', name], {'AX_ENV_HOME': str(home)}, source.read)


def test_put_writes_binary_and_archives(tmp_path):
    home, target = setup_manifest(tmp_path)
    code, result = run_put(home, b'first\x00')
    assert code == 0 and result['size'] == 6
    assert target.read_bytes() == b'first\x00'
    code, result = run_put(home, b'next')
    assert code == 0 and target.read_bytes() == b'next'
    archived = target.parent / 'archive' / result['archived']
    assert archived.read_bytes() == b'first\x00'
    assert target.stat().st_mode & 0o777 == archived.stat().st_mode & 0o777 == 0o600
    assert b'first' not in (home / 'logs/secret-put.log').read_bytes()


def test_put_rejects_invalid_inputs_and_stop(tmp_path):
    home, target = setup_manifest(tmp_path)
    assert run_put(home, b'x', 'other')[0] == 2
    assert run_put(home, b'', 'token')[0] == 2
    assert run_put(home, b'123456789')[0] == 2
    assert run_put(home, b'x', caller='launcher')[0] == 2
    (home / 'PAUSE').touch()
    assert run_put(home, b'x')[0] == 5
    assert not target.exists()


def test_put_refuses_symlink(tmp_path):
    home, target = setup_manifest(tmp_path)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'original')
    target.symlink_to(outside)
    assert run_put(home, b'new')[0] == 2
    assert outside.read_bytes() == b'original'


def test_end_to_end_fake_ssh(tmp_path):
    home, target = setup_manifest(tmp_path)
    def runner(argv, **kwargs):
        code, response = launcher.dispatch(['--caller', 'env'],
            {'AX_ENV_HOME': str(home), 'SSH_ORIGINAL_COMMAND': ' '.join(argv[-2:])},
            io.BytesIO(kwargs['input']).read)
        return SimpleNamespace(returncode=code, stdout=json.dumps(response).encode())
    adapter = make_environment(tmp_path, runner=runner)
    assert adapter.secret_put('token', b'abc')['size'] == 3
    assert target.read_bytes() == b'abc'


def test_mismatch_and_registry_size_limit(tmp_path):
    calls = []
    def runner(argv, **kwargs):
        calls.append(True)
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true,"target":"token","size":999}')
    adapter = make_environment(tmp_path, runner=runner)
    with pytest.raises(AdapterError):
        adapter.secret_put('token', b'a' * 33)
    assert not calls
    with pytest.raises(AdapterError, match='mismatch'):
        adapter.secret_put('token', b'abc')


def test_resource_service_factory_uses_registry(tmp_path, monkeypatch):
    from core.resources import app as resources_app
    from core.resources.envs import registry
    from core.resources.store import Store
    environment = {
        'id': 'compute-host', 'adapter': 'ssh-host', 'state': 'available',
        'connect': {'kind': 'ssh', 'host': 'host.example', 'user': 'user', 'port': 22},
        'conn_credential': {'slot': 'env-ssh-compute-host', 'place': 'secrets_dir/env-ssh-compute-host'},
        'secret_targets': [{'name': 'token', 'max_bytes': 8}],
    }
    monkeypatch.setattr(registry, 'load_environment_file',
                        lambda: SimpleNamespace(get=lambda key: environment if key == 'compute-host' else None))
    monkeypatch.setenv('AX_SECRETS_PATH', str(tmp_path))
    core, _, service = resources_app.build(resources_app.Config(db=':memory:'), store=Store(':memory:'), run_heartbeat_loop=False)
    assert core.state.secret_adapter_for is service.secret_adapter_for
    assert isinstance(service.secret_adapter_for('compute-host'), SSHHostAdapter)
    with pytest.raises(AdapterError):
        service.secret_adapter_for('missing')


def test_denied_target_does_not_read_input(tmp_path):
    home, _ = setup_manifest(tmp_path)
    def forbidden_read(*args):
        raise AssertionError('stdin must not be read')
    code, _ = launcher.dispatch(['secret-put', 'unknown'], {'AX_ENV_HOME': str(home)}, forbidden_read)
    assert code == 2


def test_missing_target_directory_does_not_read_input(tmp_path):
    home, _ = setup_manifest(tmp_path)
    (home / 'manifest.yaml').write_text(yaml.safe_dump({'secret_targets': [
        {'name': 'token', 'path': str(tmp_path / 'missing' / 'target'), 'mode': '600', 'max_bytes': 8}]}))
    def forbidden_read(*args):
        raise AssertionError('stdin must not be read')
    code, response = launcher.dispatch(['secret-put', 'token'], {'AX_ENV_HOME': str(home)}, forbidden_read)
    assert code == 3 and response['error']['reason'] == 'target_dir_missing'

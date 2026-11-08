import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.resources.envs.adapters.ssh_host import SSHHostAdapter

SOURCE = Path(__file__).resolve().parents[2] / 'host/env-module-min/releases/b6'


def snapshot(root):
    return {p.relative_to(root).as_posix(): ('dir' if p.is_dir() else p.read_bytes())
            for p in root.rglob('*')}


def make_adapter(tmp_path, *, missing_version=False, fail=False):
    release = tmp_path / 'release'
    shutil.copytree(SOURCE, release, ignore=shutil.ignore_patterns('__pycache__', *(['VERSION'] if missing_version else [])))
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs['input']))
        assert argv[-1] == 'version'
        assert kwargs['input'] == b''
        if fail:
            raise OSError('SSH unavailable')
        env = dict(os.environ, AX_ENV_HOME=str(tmp_path / 'unused'))
        return subprocess.run([str(release / 'bin/ax-env-run'), 'version'], input=kwargs['input'],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)

    environment = {'id': 'remote', 'adapter': 'ssh-host', 'state': 'available',
                   'connect': {'kind': 'ssh', 'host': 'host', 'user': 'user', 'port': 22},
                   'conn_credential': {'slot': 'env-ssh-remote', 'place': 'secrets_dir/env-ssh-remote'},
                   'secret_targets': []}
    return SSHHostAdapter.from_environment(environment, secrets_dir=str(tmp_path), runner=runner), release, calls


@pytest.mark.parametrize('missing,fail', [(False, False), (True, False), (False, True)])
def test_version_over_ssh_is_read_only(tmp_path, missing, fail):
    adapter, release, calls = make_adapter(tmp_path, missing_version=missing, fail=fail)
    before = snapshot(tmp_path)
    result = adapter.version()
    assert snapshot(tmp_path) == before
    assert len(calls) == 1
    assert calls[0][0][-1:] == ['version']
    if fail:
        assert result == {'version': 'unknown', 'digest': 'unknown'}
    else:
        assert result['version'] == ('unknown' if missing else '0.1.0-b6')
        assert result['digest'].startswith('sha256:') and len(result['digest']) == 71


def test_extra_arguments_remain_rejected(tmp_path):
    _, release, _ = make_adapter(tmp_path)
    entry = str(release / 'bin/ax-env-run')
    for args in (['version', 'extra'], ['probe', 'extra']):
        result = subprocess.run([entry, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        assert result.returncode != 0


def test_digest_unavailable_is_unknown(tmp_path):
    adapter, release, calls = make_adapter(tmp_path)
    (release / 'unavailable').symlink_to(release / 'missing')
    result = adapter.version()
    assert result == {'version': '0.1.0-b6', 'digest': 'unknown'}
    assert len(calls) == 1 and calls[0][0][-1:] == ['version']

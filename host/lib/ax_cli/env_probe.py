"""CLI integration for the SSH environment probe."""
import json
import os
import subprocess

from core.resources.envs.adapters.ssh_host import SSHHostAdapter
from core.resources.envs.probe import probe
from core.resources.envs.registry.loader import default_environment_path, load_environment_file


def run(host, environ):
    environment = load_environment_file(default_environment_path(environ)).get(host)
    if environment is None:
        raise ValueError('environment not found')
    adapter = SSHHostAdapter.from_environment(environment, secrets_dir=environ.get('AX_SECRETS_PATH', '/run/secrets'), environ=environ)
    executor = adapter._executor
    def execute():
        argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes',
                '-o', 'StrictHostKeyChecking=yes', '-p', str(executor.port)]
        if executor.key_path:
            argv += ['-i', executor.key_path, '-o', 'IdentitiesOnly=yes']
        argv.append(f'{executor.user}@{executor.host}')
        if not executor.key_path:
            argv.append('~/.ax-env/bin/ax-env-run')
        argv.append('probe')
        try:
            result = subprocess.run(argv, input=json.dumps({'key_comment': environment.get('connect', {}).get('key_comment')}).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False, env=dict(environ))
        except (OSError, subprocess.SubprocessError):
            return 'unknown', None
        if result.returncode == 255:
            return 'unreachable', None
        try:
            data = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            data = None
        return 'connected', data if result.returncode == 0 else None
    return probe(execute)

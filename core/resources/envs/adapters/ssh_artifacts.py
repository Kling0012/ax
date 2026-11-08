"""SSH transport with tar artifact decoding for environment jobs."""
import json
import re
import subprocess

from host.lib.envs.execution import ConnectionLost, SSHExecutor
from .artifact_tar import decode_artifact_tar


class ArtifactSSHExecutor(SSHExecutor):
    def execute(self, *, connection, command, payload=None):
        if command.name != 'fetch':
            return super().execute(connection=connection, command=command, payload=payload)
        if (connection.kind != 'ssh-host' or connection.target != self.remote_id
                or connection.credential_ref != self.credential_ref or payload is not None
                or len(command.arguments) not in (1, 2)
                or any(not isinstance(arg, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}', arg)
                       for arg in command.arguments)):
            raise ValueError('invalid SSH request')
        argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes',
                '-o', 'StrictHostKeyChecking=yes', '-p', str(self.port)]
        if self.key_path:
            argv += ['-i', self.key_path, '-o', 'IdentitiesOnly=yes']
        argv += [f'{self.user}@{self.host}']
        if not self.key_path:
            argv.append('~/.ax-env/bin/ax-env-run')
        argv += ['fetch', *command.arguments]
        try:
            result = self._runner(argv, input=b'', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=60, check=False)
        except Exception:
            raise ConnectionLost('SSH response unavailable') from None
        if result.returncode:
            try:
                error = json.loads(result.stdout)['error']['class']
            except (ValueError, KeyError, TypeError):
                raise ConnectionLost('SSH response invalid') from None
            if error not in {'rejected', 'not_found', 'busy', 'detached', 'internal'}:
                raise ConnectionLost('SSH response invalid')
            raise ValueError('environment operation failed: ' + error)
        return decode_artifact_tar(result.stdout)


class ArtifactTooLarge(ValueError):
    pass


class BoundedHibiSSHExecutor(ArtifactSSHExecutor):
    """Read a single state.json tar stream with a bounded buffer."""

    def execute(self, *, connection, command, payload=None):
        if command.name != 'fetch':
            return super().execute(connection=connection, command=command, payload=payload)
        import os
        import selectors
        import tarfile
        import time
        from axcommon.defaults import Defaults
        if (connection.kind != 'ssh-host' or connection.target != self.remote_id
                or connection.credential_ref != self.credential_ref or payload is not None
                or len(command.arguments) != 2 or command.arguments[1] != 'state.json'
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', command.arguments[0])):
            raise ValueError('invalid SSH request')
        limit = int(Defaults.load().get('hibi_body_max_kib')) * 1024
        argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes',
                '-o', 'StrictHostKeyChecking=yes', '-p', str(self.port)]
        if self.key_path:
            argv += ['-i', self.key_path, '-o', 'IdentitiesOnly=yes']
        argv += [f'{self.user}@{self.host}']
        if not self.key_path:
            argv.append('~/.ax-env/bin/ax-env-run')
        argv += ['fetch', command.arguments[0], 'state.json']
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        output = bytearray()
        checked = False
        deadline = time.monotonic() + 60
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ConnectionLost('SSH response timed out')
                    chunk = os.read(process.stdout.fileno(), min(8192, limit + 10241 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > limit + 10240:
                        raise ArtifactTooLarge('artifact archive exceeds limit')
                    if not checked and len(output) >= 512:
                        try:
                            header = tarfile.TarInfo.frombuf(bytes(output[:512]), 'utf-8', 'strict')
                        except (tarfile.TarError, ValueError, UnicodeError):
                            raise ConnectionLost('invalid artifact archive') from None
                        if header.name != 'state.json' or not header.isfile():
                            raise ConnectionLost('invalid artifact archive')
                        if header.size > limit:
                            raise ArtifactTooLarge('artifact exceeds limit')
                        checked = True
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0 or not checked:
                raise ConnectionLost('SSH response unavailable')
            return decode_artifact_tar(bytes(output))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()

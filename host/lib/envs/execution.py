"""Host-side helpers shared by the environment adapters.

The SSH transport is bound to a registered environment and accepts only the
fixed environment-module operations. Credentials are passed by reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class Connection:
    kind: str
    target: str
    credential_ref: str | None = None


@dataclass(frozen=True)
class Command:
    name: str
    arguments: tuple[str, ...] = ()

    @property
    def argv(self) -> tuple[str, ...]:
        return ("ax-env-run", self.name, *self.arguments)


class ConnectionLost(RuntimeError):
    """The transport lost its reply after an operation may have started."""


class Executor(Protocol):
    """The replaceable transport boundary used by every adapter."""

    def execute(
        self,
        *,
        connection: Connection,
        command: Command,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


class SSHExecutor:
    """SSH transport bound to one registered host."""

    def __init__(self, *, remote_id: str, host: str, user: str, port: int,
                 credential_ref: str | None, key_path: str | None = None,
                 runner=None) -> None:
        import re
        import subprocess
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', remote_id):
            raise ValueError('invalid environment reference')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', host):
            raise ValueError('invalid SSH host')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', user):
            raise ValueError('invalid SSH user')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid SSH port')
        self.remote_id, self.host, self.user, self.port = remote_id, host, user, port
        self.credential_ref, self.key_path = credential_ref, key_path
        self._runner = runner or subprocess.run

    def execute_secret(self, *, connection: Connection, command: Command, stdin: bytes) -> Mapping[str, Any]:
        import json
        import re
        import subprocess
        if (connection.kind != 'ssh-host' or connection.target != self.remote_id
                or connection.credential_ref != self.credential_ref
                or command.name != 'secret-put' or len(command.arguments) != 1
                or not re.fullmatch(r'[a-z0-9-]{1,40}', command.arguments[0])):
            raise ValueError('invalid SSH request')
        argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes',
                '-o', 'StrictHostKeyChecking=yes', '-p', str(self.port)]
        if self.key_path:
            argv += ['-i', self.key_path, '-o', 'IdentitiesOnly=yes']
        remote_command = ['secret-put', command.arguments[0]] if self.key_path else ['~/.ax-env/bin/ax-env-run', 'secret-put', command.arguments[0]]
        argv += [f'{self.user}@{self.host}', *remote_command]
        try:
            result = self._runner(argv, input=stdin, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=60, check=False)
        except Exception:
            raise ConnectionLost('SSH transport failed; outcome may be unknown') from None
        try:
            response = json.loads(result.stdout)
        except (ValueError, TypeError, UnicodeError):
            raise ConnectionLost('SSH response invalid; outcome may be unknown') from None
        if not isinstance(response, dict):
            raise ConnectionLost('SSH response invalid; outcome may be unknown')
        if result.returncode != 0 or response.get('ok') is not True:
            error = response.get('error')
            error_class = error.get('class') if isinstance(error, dict) else None
            if error_class not in {'rejected', 'not_found', 'detached', 'internal'}:
                error_class = 'transport'
            return {'ok': False, 'error': {'class': error_class}}
        return {key: response.get(key) for key in ('ok', 'target', 'size', 'mtime', 'mode', 'archived')}

    def execute(self, *, connection: Connection, command: Command,
                payload: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        import io
        import json
        import re
        import subprocess
        import tarfile

        if (connection.kind != 'ssh-host' or connection.target != self.remote_id
                or connection.credential_ref != self.credential_ref):
            raise ValueError('invalid SSH connection')
        counts = {'submit': (1,), 'status': (1,), 'logs': (2,), 'fetch': (1, 2),
                  'cancel': (1,), 'lookup': (1,), 'release': (1,), 'probe': (0,), 'version': (0,)}
        args = command.arguments
        if command.name not in counts or len(args) not in counts[command.name]:
            raise ValueError('invalid SSH operation')
        # SSH joins remote arguments into a shell command: never pass shell syntax.
        if any(not isinstance(arg, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}', arg)
               for arg in args):
            raise ValueError('invalid SSH argument')
        if command.name == 'logs' and not args[1].isdigit():
            raise ValueError('invalid log sequence')
        if command.name == 'submit':
            if not isinstance(payload, Mapping) or payload.get('job_id') != args[0]:
                raise ValueError('invalid job payload')
            spec = json.dumps(dict(payload), ensure_ascii=True, separators=(',', ':')).encode()
            if len(spec) > 1024 * 1024:
                raise ValueError('job payload too large')
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w') as archive:
                info = tarfile.TarInfo('spec.json')
                info.size = len(spec)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(spec))
            stdin = stream.getvalue()
        else:
            if payload is not None:
                raise ValueError('unexpected SSH payload')
            stdin = b''
        argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes',
                '-o', 'StrictHostKeyChecking=yes', '-p', str(self.port)]
        if self.key_path:
            argv += ['-i', self.key_path, '-o', 'IdentitiesOnly=yes']
        remote = [] if self.key_path else ['~/.ax-env/bin/ax-env-run']
        remote += [command.name, *args]
        if command.name == 'logs':
            remote = remote[:-1] + ['--since', args[1]]
        argv += [f'{self.user}@{self.host}', *remote]
        try:
            result = self._runner(argv, input=stdin, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=60, check=False)
            response = json.loads(result.stdout)
        except Exception:
            raise ConnectionLost('SSH response unavailable; outcome may be unknown') from None
        if not isinstance(response, dict):
            raise ConnectionLost('SSH response invalid; outcome may be unknown')
        if result.returncode != 0:
            error = response.get('error')
            error_class = error.get('class') if isinstance(error, dict) else None
            if error_class not in {'rejected', 'not_found', 'busy', 'detached', 'internal'}:
                raise ConnectionLost('SSH operation failed; outcome may be unknown')
            raise ValueError('environment operation failed: ' + error_class)
        return response

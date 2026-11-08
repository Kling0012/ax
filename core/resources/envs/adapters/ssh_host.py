"""SSH-host adapter with a narrowly scoped secret-put operation."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from host.lib.envs import Command
from .ssh_artifacts import ArtifactSSHExecutor

from .base import AdapterError, BaseEnvironmentAdapter


class SSHHostAdapter(BaseEnvironmentAdapter):
    adapter_kind = "ssh-host"

    def __init__(self, *, secret_targets=(), **kwargs):
        super().__init__(**kwargs)
        self._secret_targets = dict(secret_targets)

    @classmethod
    def from_environment(cls, environment: Mapping[str, Any], *, secrets_dir: str, environ=None, runner=None):
        """Bind the adapter to a validated entry from EnvironmentRegistry."""
        if environment.get('adapter') != 'ssh-host' or environment.get('state') not in ('trial', 'available'):
            raise AdapterError('environment unavailable')
        env = os.environ if environ is None else environ
        connect = environment.get('connect', {})
        credential = environment.get('conn_credential', {})
        if not isinstance(connect, Mapping) or connect.get('kind') != 'ssh' or not isinstance(credential, Mapping):
            raise AdapterError('invalid environment connection')
        def resolve(value):
            if isinstance(value, str) and re.fullmatch(r'\$\{[A-Z][A-Z0-9_]*\}', value):
                return env.get(value[2:-1], '')
            return value
        slot, place = credential.get('slot'), credential.get('place')
        key_path = None
        if isinstance(place, str) and place.startswith('secrets_dir/'):
            name = place[len('secrets_dir/'):]
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,127}', name) or name != slot:
                raise AdapterError('invalid SSH credential')
            key_path = str(Path(secrets_dir) / name)
        elif place != 'profile:tailscale-ssh':
            raise AdapterError('invalid SSH credential')
        targets = environment.get('secret_targets')
        if not isinstance(targets, list) or any(not isinstance(item, Mapping) or
            not isinstance(item.get('name'), str) or not re.fullmatch(r'[a-z0-9-]{1,40}', item['name']) or
            type(item.get('max_bytes')) is not int or not 1 <= item['max_bytes'] <= 65536
            for item in targets) or len({item['name'] for item in targets}) != len(targets):
            raise AdapterError('invalid secret targets')
        try:
            executor = ArtifactSSHExecutor(remote_id=environment['id'], host=resolve(connect.get('host')),
                user=resolve(connect.get('user')), port=int(resolve(connect.get('port'))),
                credential_ref=slot, key_path=key_path, runner=runner)
            return cls(executor=executor, remote_id=environment['id'], connection_credential=slot,
                       secret_targets=[(item['name'], item['max_bytes']) for item in targets])
        except (ValueError, TypeError, KeyError):
            raise AdapterError('invalid environment connection') from None

    def version(self) -> Mapping[str, Any]:
        try:
            response = self._call('version')
        except Exception:
            return {'version': 'unknown', 'digest': 'unknown'}
        if not isinstance(response, Mapping):
            return {'version': 'unknown', 'digest': 'unknown'}
        version, digest = response.get('version'), response.get('digest')
        return {'version': version if isinstance(version, str) and version.strip() and '\n' not in version and '\r' not in version else 'unknown',
                'digest': digest if isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', digest) else 'unknown'}

    def secret_put(self, target: str, value: bytes) -> Mapping[str, Any]:
        if not isinstance(target, str) or target not in self._secret_targets:
            raise AdapterError('target not permitted')
        if not isinstance(value, bytes) or not 0 < len(value) <= self._secret_targets[target]:
            raise AdapterError('invalid secret size')
        try:
            result = self._executor.execute_secret(connection=self.connection,
                command=Command('secret-put', (target,)), stdin=value)
        except Exception:
            raise AdapterError('secret transport failed; outcome may be unknown') from None
        if not isinstance(result, Mapping) or result.get('ok') is not True:
            raise AdapterError('secret put failed; inspect environment status')
        if result.get('target') != target or type(result.get('size')) is not int or result['size'] != len(value):
            raise AdapterError('secret put response mismatch; outcome may be unknown')
        mtime, archived = result.get('mtime'), result.get('archived')
        if (result.get('mode') != '600' or not isinstance(mtime, str) or
                not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', mtime) or
                (archived is not None and (not isinstance(archived, str) or
                    not re.fullmatch(r'[A-Za-z0-9._-]{1,255}', archived)))):
            raise AdapterError('secret put response invalid; outcome may be unknown')
        return {key: result.get(key) for key in ('ok', 'target', 'size', 'mtime', 'mode', 'archived')}


class AuditJobExecutor:
    """Run the fixed read-only audit and recover its recorded outcome."""

    def __init__(self, *, secrets_dir=None, runner=None, environ=None):
        self.secrets_dir = secrets_dir or os.environ.get('AX_SECRETS_PATH', '/run/secrets')
        self.runner, self.environ = runner, environ

    def _adapter(self, environment):
        return SSHHostAdapter.from_environment(environment, secrets_dir=self.secrets_dir,
                                                runner=self.runner, environ=self.environ)

    # Each additional entrypoint must supply its own strict validation and result handling.
    ENTRYPOINTS = {"audit-readonly": "audit", "x-user-posts": "user_posts"}

    @staticmethod
    def _validate_user_posts(spec):
        entry = spec.get("entry", {})
        args = entry.get("args") if isinstance(entry, Mapping) else None
        allowed = {"job_id", "requires", "sensitivity", "entry", "outputs", "secrets", "budget_min",
                   "stall_min", "irreversible", "scope", "idempotent", "idempotency_key"}
        if (set(spec) != allowed or spec.get("sensitivity") != "public" or spec.get("idempotent") is not False
                or set(entry) != {"kind", "name", "args"} or entry.get("kind") != "entrypoint"
                or not isinstance(args, Mapping) or set(args) != {"user", "limit", "retweets"}
                or not isinstance(args["user"], str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", args["user"])
                or type(args["limit"]) is not int or not 1 <= args["limit"] <= 20
                or type(args["retweets"]) is not bool or spec.get("outputs") != ["posts.json"]
                or spec.get("secrets") or spec.get("inputs") or spec.get("requires") != ["x-session"]
                or spec.get("irreversible") is not False or spec.get("scope") != "one"
                or type(spec.get("budget_min")) is not int or not 1 <= spec["budget_min"] <= 5
                or type(spec.get("stall_min")) is not int or not 1 <= spec["stall_min"] <= 5):
            raise AdapterError("unsupported user posts request")

    @staticmethod
    def _user_posts_result(adapter, spec, response):
        if response.get("state") == "failed":
            return {"status": "error", "error": "user_posts_failed"}
        if response.get("state") != "succeeded" or response.get("exit_code") != 0:
            raise AdapterError("user posts outcome unknown")
        artifacts = adapter.fetch(spec["job_id"], "posts.json")
        if len(artifacts) != 1 or artifacts[0].name != "posts.json" or artifacts[0].size > 1024 * 1024:
            raise AdapterError("user posts artifact unavailable")
        try:
            content = artifacts[0].content.decode("utf-8", "strict")
        except UnicodeError:
            raise AdapterError("user posts artifact invalid") from None
        return {"status": "ok", "output": {"posts.json": content}}

    @staticmethod
    def _validate(spec):
        if (not isinstance(spec, Mapping) or spec.get('entry') != {
                'kind': 'entrypoint', 'name': 'audit-readonly', 'args': {}}
                or spec.get('secrets') or spec.get('inputs')):
            raise AdapterError('unsupported audit request')

    @staticmethod
    def _result(adapter, spec, response):
        state = response.get('state')
        if state == 'failed':
            return {'status': 'error', 'error': 'audit_failed'}
        if state != 'succeeded' or response.get('exit_code') != 0:
            raise AdapterError('audit outcome unknown')
        artifacts = adapter.fetch(spec['job_id'], 'audit.tsv')
        if len(artifacts) != 1 or artifacts[0].name != 'audit.tsv':
            raise AdapterError('audit artifact unavailable')
        try:
            text = artifacts[0].content.decode('utf-8', 'strict')
            values = {}
            for line in text.splitlines():
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t', 2)
                if len(parts) != 3 or parts[0] != 'sshd_effective' or not parts[1] or parts[1] in values:
                    raise ValueError('invalid audit')
                values[parts[1]] = parts[2]
            if not values or 'passwordauthentication' not in values:
                raise ValueError('incomplete audit')
        except (ValueError, UnicodeError):
            raise AdapterError('audit artifact invalid') from None
        return {'status': 'ok', 'values': {'sshd_effective': values}}

    def _read_ssh(self, environment):
        import subprocess
        adapter = self._adapter(environment)
        executor = adapter._executor
        if not executor.key_path or not Path(executor.key_path).is_file():
            return {'status': 'error', 'error': 'audit_credential_unavailable'}
        known_hosts = Path(self.secrets_dir) / (executor.credential_ref + '-known-hosts')
        if not known_hosts.is_file():
            return {'status': 'error', 'error': 'audit_known_hosts_unavailable'}
        argv = ['ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes',
                '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known_hosts}',
                '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'IdentitiesOnly=yes',
                '-o', 'ConnectTimeout=10', '-p', str(executor.port), '-i', executor.key_path,
                f'{executor.user}@{executor.host}', 'sudo -n sshd -T']
        try:
            response = executor._runner(argv, input=b'', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=30, check=False)
            if response.returncode or len(response.stdout) > 65536:
                raise ValueError('audit unavailable')
            # OpenSSH emits these directives once per configured value. Keep
            # their type stable even when only one value is configured.
            repeated = {'listenaddress', 'hostkey', 'acceptenv'}
            values = {}
            for line in response.stdout.decode('utf-8', 'strict').splitlines():
                parts = line.split(None, 1)
                if len(parts) != 2 or not re.fullmatch(r'[a-z][a-z0-9]*', parts[0]):
                    raise ValueError('invalid audit')
                key, value = parts
                if key in repeated:
                    values.setdefault(key, []).append(value)
                elif key in values:
                    raise ValueError('invalid audit')
                else:
                    values[key] = value
            if 'passwordauthentication' not in values:
                raise ValueError('incomplete audit')
            return {'status': 'ok', 'values': {'sshd_effective': values}}
        except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError):
            return {'status': 'error', 'error': 'audit_unavailable'}

    def submit(self, environment, spec):
        entry = spec.get('entry', {}) if isinstance(spec, Mapping) else {}
        kind = self.ENTRYPOINTS.get(entry.get('name')) if isinstance(entry, Mapping) else None
        if kind == 'user_posts':
            self._validate_user_posts(spec)
            if environment.get('audit_transport') == 'ssh-readonly':
                raise AdapterError('unsupported transport')
            adapter = self._adapter(environment)
            return self._user_posts_result(adapter, spec, adapter.submit(spec))
        self._validate(spec)
        if environment.get('audit_transport') == 'ssh-readonly':
            return self._read_ssh(environment)
        adapter = self._adapter(environment)
        return self._result(adapter, spec, adapter.submit(spec))

    def status(self, environment, spec):
        entry = spec.get('entry', {}) if isinstance(spec, Mapping) else {}
        kind = self.ENTRYPOINTS.get(entry.get('name')) if isinstance(entry, Mapping) else None
        if kind == 'user_posts':
            self._validate_user_posts(spec)
            if environment.get('audit_transport') == 'ssh-readonly':
                raise AdapterError('unsupported transport')
            adapter = self._adapter(environment)
            return self._user_posts_result(adapter, spec, adapter.status(spec['job_id']))
        self._validate(spec)
        if environment.get('audit_transport') == 'ssh-readonly':
            return {'status': 'error', 'error': 'audit_outcome_unknown'}
        adapter = self._adapter(environment)
        return self._result(adapter, spec, adapter.status(spec['job_id']))


class HibiJobExecutor:
    """Run only the registered Hibi read-only entrypoint over the job protocol."""

    def __init__(self, *, secrets_dir=None, runner=None, environ=None):
        self.secrets_dir = secrets_dir or os.environ.get('AX_SECRETS_PATH', '/run/secrets')
        self.runner, self.environ = runner, environ

    def _adapter(self, environment):
        from .ssh_artifacts import BoundedHibiSSHExecutor
        adapter = SSHHostAdapter.from_environment(environment, secrets_dir=self.secrets_dir,
                                                   runner=self.runner, environ=self.environ)
        original = adapter._executor
        adapter._executor = BoundedHibiSSHExecutor(remote_id=original.remote_id, host=original.host,
            user=original.user, port=original.port, credential_ref=original.credential_ref,
            key_path=original.key_path, runner=self.runner)
        return adapter

    @staticmethod
    def _validate(spec):
        if (not isinstance(spec, Mapping) or spec.get('entry') !=
                {'kind': 'entrypoint', 'name': 'hibi-state', 'args': {}}
                or spec.get('secrets') or spec.get('inputs')
                or spec.get('outputs') != ['state.json']
                or spec.get('requires') != ['hibi-state-read']
                or spec.get('budget_min') != 2 or spec.get('irreversible') is not False):
            raise AdapterError('unsupported Hibi request')

    @staticmethod
    def _result(adapter, spec, response):
        import json
        from axcommon.defaults import Defaults
        if response.get('state') in ('failed', 'timed_out', 'lost', 'rejected', 'stalled'):
            return {'status': 'error', 'error': 'hibi_execution_failed'}
        if response.get('state') != 'succeeded' or response.get('exit_code') != 0:
            raise AdapterError('Hibi outcome unknown')
        artifacts = adapter.fetch(spec['job_id'], 'state.json')
        limit = int(Defaults.load().get('hibi_body_max_kib')) * 1024
        if len(artifacts) != 1 or artifacts[0].name != 'state.json' or artifacts[0].size > limit:
            raise AdapterError('invalid Hibi artifact')
        try:
            data = json.loads(artifacts[0].content)
            from axcommon import schemas
            if schemas.errors('hibi-state', data):
                raise ValueError('invalid Hibi artifact')
            data['now'] = {k: v for k, v in data['now'].items() if k in ('state', 'since', 'as_of')}
            data['day'] = {'date': data['day'].get('date'), 'segments': [
                {k: v for k, v in segment.items() if k in ('start', 'end', 'state', 'category')}
                for segment in data['day'].get('segments', [])]}
        except (ValueError, UnicodeError):
            raise AdapterError('invalid Hibi artifact') from None
        return {'status': 'ok', 'artifacts': {'state.json': data}}

    def submit(self, environment, spec):
        self._validate(spec)
        adapter = self._adapter(environment)
        from .ssh_artifacts import ArtifactTooLarge
        try:
            return self._result(adapter, spec, adapter.submit(spec))
        except ArtifactTooLarge:
            return {'status': 'error', 'error': 'artifact_too_large'}

    def status(self, environment, spec):
        self._validate(spec)
        adapter = self._adapter(environment)
        from .ssh_artifacts import ArtifactTooLarge
        try:
            return self._result(adapter, spec, adapter.status(spec['job_id']))
        except ArtifactTooLarge:
            return {'status': 'error', 'error': 'artifact_too_large'}


class RestrictedJobExecutor:
    def __init__(self):
        self.audit = AuditJobExecutor()
        self.hibi = HibiJobExecutor()

    def _executor(self, spec):
        name = spec.get('entry', {}).get('name')
        if name == 'audit-readonly':
            return self.audit
        if name == 'hibi-state':
            return self.hibi
        raise AdapterError('unsupported entrypoint')

    def submit(self, environment, spec):
        return self._executor(spec).submit(environment, spec)

    def status(self, environment, spec):
        return self._executor(spec).status(environment, spec)

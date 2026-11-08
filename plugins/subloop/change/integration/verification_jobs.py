"""Durable launcher job claims for a verification run.

The caller supplies a fresh run ID per request and the registered host binding.
An uncertain prepare is reconciled by ID; it is never submitted twice.
"""
from __future__ import annotations

import hashlib
import json
import re

from .ports import DependencyUnavailable

_IDENT = re.compile(r'^[a-z0-9-]{1,40}$')
_SHA = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
_FIELDS = ('project', 'target', 'revision', 'run_id', 'attempt', 'recipe_id', 'recipe_snapshot_id', 'stage', 'step')


def job_id_for(request):
    canonical = json.dumps(request, sort_keys=True, separators=(',', ':'))
    return 'verify-' + hashlib.sha256(canonical.encode()).hexdigest()[:32]


class PoolVerificationTransport:
    """Use the worker-pool work interface with its existing component token."""
    def __init__(self, pool):
        self.pool = pool

    def job_verify_prepare(self, job_id, request):
        return self.pool.post('/v1/verification/jobs/' + job_id, request)

    def job_verify_result(self, job_id):
        return self.pool.get('/v1/verification/jobs/' + job_id)


class VerificationJobs:
    def __init__(self, launcher, journal):
        self.launcher, self.journal = launcher, journal

    def poll(self, request):
        if not isinstance(request, dict) or set(request) != set(_FIELDS):
            raise DependencyUnavailable('verification_binding_invalid')
        if any(not isinstance(request[k], str) or not _IDENT.fullmatch(request[k])
               for k in ('target', 'run_id', 'attempt', 'recipe_id', 'recipe_snapshot_id', 'step')):
            raise DependencyUnavailable('verification_binding_invalid')
        if (not isinstance(request['project'], str) or not request['project'] or
                not isinstance(request['revision'], str) or not _SHA.fullmatch(request['revision']) or
                request['stage'] not in {'dependency', 'verify'}):
            raise DependencyUnavailable('verification_binding_invalid')
        job_id = job_id_for(request)
        key = 'verification-job:' + job_id
        op = self.journal.operation(key)
        if op is None and self.journal.claim(key, 'verification_job', {'request': request, 'job_id': job_id}):
            try:
                self.launcher.job_verify_prepare(job_id, request)
            except Exception:
                # The launcher may have accepted the write. Result lookup is the
                # only safe next action, including after a process restart.
                self.journal.finish(key, 'lost', {'reason': 'prepare_unconfirmed'})
            else:
                self.journal.finish(key, 'retry_wait', {'job_id': job_id})
        op = self.journal.operation(key)
        if not op or op['body'].get('request') != request or op['body'].get('job_id') != job_id:
            raise DependencyUnavailable('verification_identity_mismatch')
        try:
            result = self.launcher.job_verify_result(job_id)
        except Exception as exc:
            raise DependencyUnavailable('verification_result_unconfirmed') from exc
        if (not isinstance(result, dict) or result.get('job_id') != job_id or
                any(result.get(field) != request[field] for field in _FIELDS)):
            raise DependencyUnavailable('verification_identity_mismatch')
        state = result.get('state')
        if state in {'starting', 'running'}:
            return {'status': 'pending', 'job_id': job_id}
        if state != 'exited':
            raise DependencyUnavailable('verification_result_unconfirmed')
        # This class does not certify the environment or execution. The caller
        # must check host attestation and complete logs before accepting evidence.
        return {'status': 'exited', 'job_id': job_id, 'result': result}


class VerificationRuns:
    """Persist a host recipe snapshot for one scoped verification request."""
    def __init__(self, pool, journal):
        self.pool, self.journal = pool, journal

    def begin(self, scope, *, project, target, recipe_id, allowed_hosts):
        from uuid import uuid4
        from ..verification.policy import GateError, hosts
        if (not isinstance(scope, dict) or not scope or not isinstance(project, str) or not project or
                any(not isinstance(x, str) or not _IDENT.fullmatch(x) for x in (target, recipe_id))):
            raise DependencyUnavailable('verification_binding_invalid')
        try:
            allowed = hosts(allowed_hosts)
            if len(allowed) != len(set(allowed)):
                raise GateError('outbound_allowlist_invalid')
        except GateError as exc:
            raise DependencyUnavailable('verification_binding_invalid') from exc
        body = {'project': project, 'target': target, 'recipe_id': recipe_id}
        scope_text = json.dumps({'scope': scope, 'binding': body, 'allowed_hosts': allowed}, sort_keys=True, separators=(',', ':'))
        key = 'verification-run:' + hashlib.sha256(scope_text.encode()).hexdigest()
        op = self.journal.operation(key)
        if op is None:
            request_id = 'recipe-' + uuid4().hex
            self.journal.claim(key, 'verification_run', {'scope': scope, 'binding': body,
                                                          'allowed_hosts': allowed, 'request_id': request_id})
            op = self.journal.operation(key)
        if not op or op['body'].get('scope') != scope or op['body'].get('binding') != body or op['body'].get('allowed_hosts') != allowed:
            raise DependencyUnavailable('verification_identity_mismatch')
        request_id = op['body'].get('request_id')
        if not isinstance(request_id, str) or not _IDENT.fullmatch(request_id):
            raise DependencyUnavailable('verification_binding_invalid')
        result = op.get('result') or {}
        if op['state'] != 'succeeded':
            try:
                result = self.pool.post('/v1/verification/recipes/' + request_id, body)
            except Exception as exc:
                raise DependencyUnavailable('verification_recipe_unconfirmed') from exc
            if (not isinstance(result, dict) or any(result.get(k) != v for k, v in body.items()) or
                    result.get('request_id') != request_id or
                    not isinstance(result.get('recipe_snapshot_id'), str) or not _IDENT.fullmatch(result['recipe_snapshot_id']) or
                    not isinstance(result.get('recipe_version'), str) or not result['recipe_version']):
                raise DependencyUnavailable('verification_recipe_unconfirmed')
            if result.get('prestart_isolation_enforced') is not True:
                raise DependencyUnavailable('verification_prestart_isolation_unconfirmed')
            try:
                received = hosts(result.get('allow_hosts'))
            except GateError as exc:
                raise DependencyUnavailable('verification_recipe_unconfirmed') from exc
            if received != allowed or len(received) != len(set(received)):
                raise DependencyUnavailable('verification_allowlist_mismatch')
            self.journal.finish(key, 'succeeded', result)
        if result.get('prestart_isolation_enforced') is not True:
            raise DependencyUnavailable('verification_prestart_isolation_unconfirmed')
        return {'run_id': request_id, 'recipe_snapshot_id': result['recipe_snapshot_id'],
                'recipe_version': result['recipe_version'], 'allow_hosts': allowed}

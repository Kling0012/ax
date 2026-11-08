"""Resource API client for the security monitor.

The server must return a per-environment ``results`` mapping in the terminal
job response. Unknown or incomplete responses are never treated as clean.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Mapping

from axcommon.client import ApiClient


class EnvironmentJobsClient:
    def __init__(self, base_url, token, *, transport=None, timeout=10, wait_seconds=150,
                 poll_seconds=5):
        if not isinstance(token, str) or not token.strip():
            raise ValueError('environment jobs credential is required')
        if wait_seconds <= 0 or poll_seconds <= 0:
            raise ValueError('poll intervals must be positive')
        self.api = ApiClient(base_url, token, transport=transport, timeout=timeout)
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds

    def run(self, *, requires, scope, entry):
        if requires != ['security-audit'] or scope != 'each' or entry != 'audit-readonly':
            raise ValueError('unsupported environment job')
        job_id = 'security-watch-' + uuid.uuid4().hex
        self.api.post('/v1/env/jobs', {
            'job_id': job_id, 'requires': requires, 'scope': scope,
            'sensitivity': 'private', 'entry': {'kind': 'entrypoint', 'name': entry, 'args': {}},
            'outputs': ['audit.tsv'], 'secrets': [], 'budget_min': 2, 'stall_min': 2,
            'irreversible': False, 'idempotent': True, 'idempotency_key': job_id,
        })
        deadline = time.monotonic() + self.wait_seconds
        while True:
            response = self.api.get('/v1/env/jobs/' + job_id)
            if not isinstance(response, Mapping):
                raise ValueError('invalid environment job response')
            state = response.get('state')
            if state not in ('queued', 'running'):
                results = response.get('results')
                if (not isinstance(results, Mapping) or state not in ('succeeded', 'failed')
                        or (state == 'failed' and not any(
                            not isinstance(result, Mapping) or result.get('status') != 'ok'
                            for result in results.values()))):
                    raise ValueError('environment job results unavailable')
                return results
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('environment job timed out')
            time.sleep(min(self.poll_seconds, remaining))


def create_monitor(database, *, resources_url, token, tumu, notifier, ledger, **client_options):
    """Construct the monitor with its authenticated resource API client."""
    from . import Monitor
    jobs = EnvironmentJobsClient(resources_url, token, **client_options)
    return Monitor(database, tumu=tumu, environment_jobs=jobs, notifier=notifier, ledger=ledger)

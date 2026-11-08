"""One-shot Instabox execution through the existing path J policy."""
from __future__ import annotations

from .composition import bind_adapter
from .instabox import InstaboxDriver


class SandboxRun:
    """Bind one configured box to trusted assignment and policy providers."""

    def __init__(self, *, executable, remote_id, policy, resolve_job, repo_is_public,
                 state_store=None, box_bindings=None, timeout=600, cancel_grace=2):
        driver = InstaboxDriver(executable, timeout=timeout, cancel_grace=cancel_grace)
        self._adapter = bind_adapter(driver, remote_id=remote_id, state_store=state_store,
                                     box_bindings=box_bindings, policy=policy, resolve_job=resolve_job,
                                     repo_is_public=repo_is_public)

    def run(self, spec):
        result = dict(self._adapter.submit(spec))
        if result.get('reason') == 'timeout':
            result.update(local_state='stopped', remote_state='unknown')
        if result.get('state') == 'succeeded':
            result['logs'] = self._adapter.logs(spec['job_id'])
        return result

    def cancel(self, job_id):
        """Request cancellation; run returns the final local and remote states."""
        return self._adapter.cancel(job_id)

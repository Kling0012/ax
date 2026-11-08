"""Policy-bound HTTP gate admission; all job facts come from a trusted resolver."""
from collections.abc import Mapping

from core.resources.box.protocol import BoxError
from .gate import GateDenied


class PolicyGate:
    def __init__(self, gate, *, policy, resolve_job, repo_is_public):
        self.gate = gate
        self.policy = policy
        self.resolve_job = resolve_job
        self.repo_is_public = repo_is_public

    def _authenticate(self, authorization):
        self.gate._authenticate(authorization)

    def issue(self, env_id, authorization, request):
        self._authenticate(authorization)
        if not isinstance(request, dict) or not isinstance(request.get('job_id'), str):
            raise GateDenied('bad_request')
        try:
            context = self.resolve_job(env_id, request['job_id'])
            if not isinstance(context, Mapping):
                raise GateDenied('job_unavailable')
            spec = context['spec']
            if spec.get('sensitivity') != request.get('sensitivity'):
                raise GateDenied('sensitivity_denied')
            self.policy.check_job(context['environment'], spec,
                                  required_credentials=context['required_credentials'],
                                  repo_is_public=self.repo_is_public)
            operations = context['operations']
            if not isinstance(operations, list) or not operations:
                raise GateDenied('tools_missing')
            for operation in operations:
                self.policy.authorize_call(operation['name'], operation['args'], path='G',
                                           root=context['root'], job_id=request['job_id'])
                tool = self.policy.tool_spec(operation['name'], path='G')
                if tool['capability'] not in request.get('tools', []):
                    raise GateDenied('tools_not_available')
        except GateDenied:
            raise
        except BoxError as exc:
            # Only a fixed vocabulary may cross the HTTP boundary.
            code = exc.code if exc.code in {'credential_invalid', 'credential_missing',
                    'sensitivity_denied', 'repo_not_public', 'scan_blocked',
                    'forbidden_command', 'tool_denied'} else 'policy_denied'
            raise GateDenied(code) from None
        except Exception:
            raise GateDenied('policy_denied') from None
        return self.gate.issue(env_id, authorization, request)

    def revoke(self, env_id, token_id, authorization):
        return self.gate.revoke(env_id, token_id, authorization)

    def validate(self, env_id, token):
        return self.gate.validate(env_id, token)

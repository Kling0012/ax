"""B18 composition helpers. Callers supply trusted registry and scanner inputs."""
from core.resources.envs.adapters.base import AdapterError
from core.resources.envs.adapters.cloud_sandbox import CloudSandboxAdapter
from .admission import PolicyGate


def bind_gate(gate, *, policy, resolve_job, repo_is_public):
    return PolicyGate(gate, policy=policy, resolve_job=resolve_job, repo_is_public=repo_is_public)


def bind_adapter(driver, *, remote_id, state_store, policy, resolve_job, repo_is_public, box_bindings=None):
    def authorize(spec):
        try:
            if box_bindings is not None and not box_bindings.box_assigned(remote_id, spec['job_id']):
                raise AdapterError('sandbox box is not assigned to job')
            context = resolve_job(remote_id, spec['job_id'])
            # The registry, not the request, supplies the authoritative job.
            if context['spec'] != spec:
                raise AdapterError('sandbox job differs from registry')
            policy.check_job(context['environment'], spec,
                             required_credentials=context['required_credentials'],
                             repo_is_public=repo_is_public)
            for operation in context['operations']:
                policy.authorize_call(operation['name'], operation['args'], path='J',
                                      root=context['root'], job_id=spec['job_id'])
        except AdapterError:
            raise
        except Exception:
            raise AdapterError('sandbox policy rejected job') from None
        return True
    return CloudSandboxAdapter(driver=driver, remote_id=remote_id,
                               scan_outbound=authorize, scan_inbound=policy.scan_inbound,
                               state_store=state_store)

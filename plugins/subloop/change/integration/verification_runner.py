"""Convert completed host verification receipts to the existing runner contract."""
from __future__ import annotations

from copy import deepcopy

from ..verification.policy import GateError
from .ports import DependencyUnavailable


class HostVerificationRunner:
    def __init__(self, jobs, *, run=None, project=None, target=None, recipe_id=None):
        self.jobs, self.run = jobs, run
        self.project, self.target, self.recipe_id = project, target, recipe_id

    def bind_invocation(self, invocation):
        from .verification_jobs import job_id_for
        if not isinstance(self.run, dict):
            raise GateError('verification_binding_unavailable')
        request = {'project': self.project, 'target': self.target, 'recipe_id': self.recipe_id,
                   'recipe_snapshot_id': self.run['recipe_snapshot_id'], 'run_id': self.run['run_id'],
                   'revision': invocation['subject_sha'], 'attempt': invocation['attempt'],
                   'stage': invocation['stage'], 'step': invocation['validation_kind']}
        return {**invocation, 'job_id': job_id_for(request), 'launcher_request': request}

    def prepare(self, invocation):
        binding = invocation.get('launcher_request')
        if not isinstance(binding, dict):
            raise GateError('verification_binding_unavailable')
        try:
            receipt = self.jobs.poll(binding)
        except DependencyUnavailable as exc:
            raise GateError(str(exc)) from exc
        if receipt.get('job_id') != invocation.get('job_id'):
            raise GateError('result_identity_mismatch')
        if receipt.get('status') == 'pending':
            raise GateError('verification_pending')
        result = receipt.get('result')
        if not isinstance(result, dict):
            raise GateError('evidence_incomplete')
        expected = {'revision': invocation.get('subject_sha'), 'runner': invocation.get('environment'),
                    'recipe_version': invocation.get('recipe_version'), 'command': invocation.get('command'),
                    'stage': invocation.get('stage')}
        if (any(result.get(k) != v for k, v in expected.items()) or
                result.get('state') != 'exited' or result.get('isolation_verified') is not True or
                result.get('executed') is not True or result.get('timed_out') is not False or
                result.get('oom_killed') is not False or result.get('log_complete') is not True or
                result.get('output_truncated') is not False or type(result.get('exit_code')) is not int or
                not isinstance(result.get('log'), str) or not isinstance(result.get('started_at'), str) or
                not isinstance(result.get('ended_at'), str)):
            raise GateError('verification_unconfirmed')
        network = 'allowlist' if invocation['stage'] == 'dependency' else 'none'
        if result.get('network_mode') != network or result.get('allow_hosts') != invocation.get('allow_hosts'):
            raise GateError('network_unconfirmed')
        required = ('home_absent', 'credentials_absent', 'host_config_absent', 'network_enforced')
        if any(result.get(field) is not True for field in required):
            raise GateError('isolation_unconfirmed')
        return {'available': True, 'job_id': invocation['job_id'], 'sha': invocation['subject_sha'],
                'environment': invocation['environment'], 'isolation_verified': result['isolation_verified'],
                **{field: result[field] for field in required}, 'network_mode': network,
                'allow_hosts': invocation['allow_hosts'], 'host_result': deepcopy(result)}

    def execute(self, invocation, prepared):
        result = prepared.get('host_result')
        if not isinstance(result, dict) or result.get('job_id') != invocation.get('job_id'):
            raise GateError('result_identity_mismatch')
        return {'job_id': result['job_id'], 'sha': result['revision'], 'started_at': result['started_at'],
                'ended_at': result['ended_at'], 'exit_code': result['exit_code'], 'timed_out': False,
                'complete': True, 'executed': True, 'output_truncated': False,
                'output_tail': result['log'], 'test_names': None, 'failure_evidence': {}}


class HostedVerification:
    """Bind registered targets and current GitHub facts to host verification."""
    def __init__(self, pool, journal, registry, github, *, scrub):
        from .verification_jobs import PoolVerificationTransport, VerificationJobs, VerificationRuns
        self.registry, self.github, self.scrub = registry, github, scrub
        self.runs = VerificationRuns(pool, journal)
        self.jobs = VerificationJobs(PoolVerificationTransport(pool), journal)

    def verify(self, item, target, *, head_sha, base_sha):
        from ..collection.models import ItemKey
        from ..verification import load_recipes, run_verification
        stopped = {'status': 'parked', 'reason': 'verify_unavailable', 'evidence': [], 'comparisons': [], 'unperformed': []}
        try:
            key = ItemKey(**{k: item[k] for k in ('provider', 'issue_repo', 'kind', 'number')})
            registered = self.registry.resolve(key)
            if (registered is None or not registered.project or not registered.launcher_target or
                    not registered.launcher_recipe_id or item.get('project') != registered.project or
                    item.get('implementation_repo', '').lower() != registered.implementation_repo or
                    target.get('implementation_repo', '').lower() != registered.implementation_repo):
                raise DependencyUnavailable('verification_binding_unavailable')
            snapshot = self.github.view(item, target)
            if (snapshot.get('snapshot_type') == 'issue' or snapshot.get('head_sha') != head_sha or
                    snapshot.get('base_sha') != base_sha or not isinstance(snapshot.get('input_revision'), str) or
                    snapshot.get('files_complete') is not True):
                raise DependencyUnavailable('verification_snapshot_unavailable')
            recipe_ref = registered.test_recipe
            if not isinstance(recipe_ref, str):
                raise DependencyUnavailable('verification_recipe_unconfigured')
            recipe_name = recipe_ref.rsplit('#', 1)[-1]
            recipes = load_recipes()
            recipe = recipes.get(recipe_name)
            if not isinstance(recipe, dict):
                raise DependencyUnavailable('verification_recipe_unconfigured')
            untrusted = item.get('author') not in registered.trusted_authors
            # Host accepts only registered check IDs, never a synthesized suite.
            if untrusted and not isinstance(recipe.get('all'), dict):
                raise DependencyUnavailable('registered_check_unavailable')
            scope = {'item': key.as_dict(), 'repo': registered.implementation_repo,
                     'head_sha': head_sha, 'base_sha': base_sha, 'input_revision': snapshot['input_revision'],
                     'recipe': recipe, 'untrusted': untrusted, 'forbidden': registered.untrusted_forbidden_paths}
            run = self.runs.begin(scope, project=registered.project, target=registered.launcher_target,
                                  recipe_id=registered.launcher_recipe_id, allowed_hosts=registered.outbound_allowlist)
            if run['recipe_version'] != recipe.get('version'):
                raise DependencyUnavailable('verification_recipe_mismatch')
            runner = HostVerificationRunner(self.jobs, run=run, project=registered.project,
                                            target=registered.launcher_target, recipe_id=registered.launcher_recipe_id)
            request = {'repo': registered.implementation_repo, 'head_sha': head_sha, 'base_sha': base_sha,
                       'untrusted': untrusted, 'test_recipe': recipe_ref, 'files_complete': True,
                       'changed_files': snapshot['changed_files'], 'untrusted_forbidden_paths': registered.untrusted_forbidden_paths,
                       'outbound_allowlist': registered.outbound_allowlist}
            return run_verification(request, runner, scrub=self.scrub, recipes=recipes)
        except (DependencyUnavailable, ValueError, TypeError, KeyError) as exc:
            reason = str(exc) if isinstance(exc, DependencyUnavailable) else 'verification_unavailable'
            return {**stopped, 'reason': reason}
        except Exception:
            return stopped

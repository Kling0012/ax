"""Convert a validated release batch to a resources env.run job."""
from __future__ import annotations

from axcommon.client import ApiError, Unavailable

from runtime.subloop.nodeapi import bug, error, ok
from runtime.subloop.nodelib import api_error_out

schema = {
    'type': 'object',
    'properties': {'product': {'type': 'object'}, 'batch': {'type': 'object'}, 'plan': {'type': 'object'}},
    'required': ['product', 'batch', 'plan'], 'additionalProperties': False,
}
META = {'statuses': ['ok', 'bug', 'error'], 'launching': True}


def spec(product, batch, plan):
    entry = product['entry']
    if entry.get('kind') != 'entrypoint' or entry.get('name') != 'release-both':
        raise NotImplementedError('environment entrypoint is not defined')
    note = plan['notes']['languages']
    return {
        'job_id': f"release-{batch['batch_id'].lower()}", 'requires': entry['requires'], 'sensitivity': 'private',
        'entry': {'kind': 'entrypoint', 'name': entry['name'], 'args': {
            'target_sha': batch['target_sha'], 'v': plan['version'],
            'headline': product['entry']['headline'], 'desc_en': note['en'], 'desc_ja': note['ja'],
            'workshop': '1', 'discord': '1'}},
        'inputs': [{'ref': f"git:{product['repo']}@{batch['target_sha']}"}],
        'outputs': ['result.json'], 'secrets': product['entry']['credentials'],
        'budget_min': product['entry']['timeout_min'], 'stall_min': 15, 'irreversible': True,
        'user_requested': False, 'scope': 'one', 'idempotent': False,
        'idempotency_key': f"release:{product['id']}:{plan['version']}",
    }


async def run(ctx, inp):
    product, batch, plan = inp['product'], inp['batch'], inp['plan']
    try:
        job = spec(product, batch, plan)
    except NotImplementedError:
        return error('release_entry_undefined', '製品登録簿の環境入口が未定義', expected=True)
    if ctx.svc.resources is None:
        return error('resources_unavailable', 'resources への接続が無い')
    await ctx.launching(f"env-run:{job['job_id']}", {'job_id': job['job_id'], 'target_sha': batch['target_sha']})
    try:
        response = await ctx.call(ctx.svc.resources.post, '/v1/env/jobs', job)
    except ApiError as exc:
        if exc.status == 409:
            return error('release_job_already_submitted', '公開ジョブは照合が必要', expected=True,
                         data={'job_id': job['job_id']})
        return api_error_out(exc)
    except Unavailable:
        return error('resources_unavailable', 'resources へ接続できない', expected=True)
    return ok({'job_id': job['job_id'], 'submission': response, 'target_sha': batch['target_sha']})


async def reconcile(ctx, key):
    job_id = key.split(':', 1)[1]
    try:
        job = await ctx.call(ctx.svc.resources.get, f'/v1/env/jobs/{job_id}')
    except ApiError as exc:
        return {'found': False} if exc.status == 404 else None
    except Unavailable:
        return None
    if not isinstance(job, dict):
        return None
    return {'found': True, 'out': ok({'job_id': job_id, 'state': job.get('state')})}

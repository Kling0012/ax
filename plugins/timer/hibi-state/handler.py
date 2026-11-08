"""Copy the validated Hibi state into the ledger through its work port."""
from __future__ import annotations

import asyncio
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from axcommon import schemas, timeutil
from axcommon.client import ApiError
from axcommon.defaults import Defaults


async def run(ctx):
    resources, ledger = ctx.clients['resources'], ctx.clients['ledger']
    interval = ctx.manifest.get('schedule', {}).get('every_s', 60)
    now = ctx.now()
    slot = int(now.timestamp()) // interval
    date = now.astimezone(ZoneInfo('Asia/Tokyo')).date()
    stamp = timeutil.format_ts(now)

    def observe(target, result, as_of=stamp):
        ctx.emit('automation.observed', target, {'plugin_id': 'hibi-state', 'target_id': target, 'as_of': as_of, 'result': result})

    async def failed(code):
        observe('focus', 'failed:' + code)
        if code == 'schema':
            return {'result': 'failed:schema'}
        count = ctx.store.get_meta('hibi-state:failures', 0) + 1
        ctx.store.set_meta('hibi-state:failures', count)
        if count >= Defaults.load().get('hibi_fail_streak') and not ctx.store.get_meta('hibi-state:todo', False):
            try:
                await asyncio.to_thread(ledger.post, '/v1/todos', {'title': 'Hibi の状態を取り込めない', 'state': 'todo', 'owner': 'system'})
                ctx.store.set_meta('hibi-state:todo', True)
            except Exception:
                pass
        return {'result': 'failed:' + code}

    if ctx.store.get_meta('hibi-state:slot') == slot:
        observe('focus', 'skipped_busy')
        return {'result': 'skipped_busy'}
    ctx.store.set_meta('hibi-state:slot', slot)
    job_id = 'hibi-state-' + now.astimezone(timezone.utc).strftime('%Y%m%d%H%M')
    job = {'job_id': job_id, 'requires': ['hibi-state-read'], 'sensitivity': 'private',
           'entry': {'kind': 'entrypoint', 'name': 'hibi-state', 'args': {}}, 'outputs': ['state.json'],
           'secrets': [], 'budget_min': 2, 'stall_min': 2, 'irreversible': False, 'scope': 'one',
           'idempotent': True, 'idempotency_key': f'hibi-state:{slot}'}
    try:
        await asyncio.to_thread(resources.post, '/v1/env/jobs', job)
        deadline = asyncio.get_running_loop().time() + 150
        while True:
            status = await asyncio.to_thread(resources.get, f'/v1/env/jobs/{job_id}')
            state = status.get('state')
            if state not in ('queued', 'running'):
                break
            if asyncio.get_running_loop().time() >= deadline:
                return await failed('timed_out')
            await asyncio.sleep(5)
        if state != 'succeeded':
            return await failed(state if state in ('failed', 'timed_out', 'stalled', 'lost', 'rejected') else 'job')
        data = await asyncio.to_thread(resources.get, f'/v1/env/jobs/{job_id}/artifacts/state.json')
    except Exception:
        return await failed('resources')
    try:
        if schemas.errors('hibi-state', data):
            return await failed('schema')
        fetched = timeutil.parse_ts(data['fetched_at'])
        now_data, day = data['now'], data['day']
        if now_data['state'] == 'unknown' or (now - fetched).total_seconds() > 2 * interval:
            observe('focus', 'skipped_unknown')
            return {'result': 'skipped_unknown'}
        focus = {k: now_data[k] for k in ('state', 'since', 'as_of')}
        day_date = day.get('date')
        daily = {'date': day_date, 'tz': 'Asia/Tokyo', 'as_of': data['fetched_at'], 'segments': [{k: v for k, v in segment.items() if k != 'session_title'}
                            for segment in day.get('segments', [])]}
        requests = [('focus', '/v1/focus-state', focus)]
        if day_date in (date.isoformat(), (date - timedelta(days=1)).isoformat()) and daily['segments']:
            requests.append(('daily:' + day_date, '/v1/daily-state/' + day_date, daily))
    except (KeyError, ValueError, TypeError):
        return await failed('schema')
    errors = []
    for target, path, body in requests:
        try:
            result = await asyncio.to_thread(ledger.put, path, body)
            observe(target, 'stored' if result.get('stored') else 'unchanged', body['as_of'])
        except ApiError as e:
            if e.status == 409 and e.code == 'stale_as_of':
                observe(target, 'unchanged', body['as_of'])
            else:
                observe(target, 'failed:ledger', body['as_of'])
                errors.append('ledger')
        except Exception:
            observe(target, 'failed:ledger', body['as_of'])
            errors.append('ledger')
    if errors:
        return await failed('ledger')
    ctx.store.set_meta('hibi-state:failures', 0)
    ctx.store.set_meta('hibi-state:todo', False)
    return {'result': 'stored'}

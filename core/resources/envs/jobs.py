"""Authenticated environment job API with an injectable execution boundary."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path

import yaml
from starlette.responses import JSONResponse
from starlette.routing import Route

from axcommon import creds
from axcommon.http import json_error, read_json
from core.ledger.workauth import plugin_table
from .contracts import EnvironmentContractError, EnvironmentJob, SelectionRequest
from .registry import load_environment_file
from .selection.select import select

_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,79}\Z')


def manifest_for(plugin_id):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', plugin_id):
        return None
    root = Path(__file__).resolve().parents[3] / 'plugins'
    paths = list(root.glob('*/' + plugin_id + '/manifest.yaml'))
    if len(paths) != 1:
        return None
    try:
        manifest = yaml.safe_load(paths[0].read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError):
        return None
    return manifest if isinstance(manifest, dict) and manifest.get('id') == plugin_id else None


class Jobs:
    def __init__(self, *, tokens=None, manifests=None, environments=None, executor=None, reconcile_interval=15):
        self.tokens = tokens if tokens is not None else plugin_table()
        self.manifests = manifests or manifest_for
        self.environments = environments or (lambda: load_environment_file().environments)
        if reconcile_interval <= 0:
            raise ValueError("reconcile interval must be positive")
        self.executor = executor
        self.reconcile_interval = reconcile_interval
        self.store = None
        self._runner = None
        self._reconcile_task = None
        self._tasks = set()

    def identify(self, request):
        self.tokens.reload_if_changed()
        return self.tokens.identify(creds.parse_bearer(request.headers.get('authorization')))

    def permitted(self, owner, operation, job=None):
        try:
            manifest = self.manifests(owner)
            rules = manifest.get('env_jobs', {}) if isinstance(manifest, dict) else {}
            if not isinstance(rules, dict):
                return False
            for key in ('operations', 'entrypoints', 'requires'):
                if not isinstance(rules.get(key), list) or not all(isinstance(x, str) for x in rules[key]):
                    return False
            secrets = manifest.get('secrets', [])
            if not isinstance(secrets, list) or not all(isinstance(x, str) for x in secrets):
                return False
            if operation not in rules['operations']:
                return False
            if job is not None:
                entry = job.entry
                return (entry.get('kind') == 'entrypoint'
                        and entry.get('name') in rules.get('entrypoints', [])
                        and set(job.requires) <= set(rules.get('requires', []))
                        and set(job.secrets) <= set(secrets))
            return True
        except (TypeError, ValueError):
            return False

    def bind(self, store):
        self.store = store
        with store._lock:
            store._conn.executescript("""
                CREATE TABLE IF NOT EXISTS env_jobs (
                    job_id TEXT PRIMARY KEY, owner TEXT NOT NULL, spec TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','unknown'))
                );
                CREATE TABLE IF NOT EXISTS env_job_results (
                    job_id TEXT NOT NULL, env_id TEXT NOT NULL, state TEXT NOT NULL,
                    result TEXT, PRIMARY KEY(job_id, env_id),
                    FOREIGN KEY(job_id) REFERENCES env_jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS env_job_results_env ON env_job_results(env_id, state);
            """)

    def _aggregate(self, db, job_id):
        states = [r[0] for r in db.execute('SELECT state FROM env_job_results WHERE job_id=?', (job_id,))]
        state = ('running' if 'running' in states else 'queued' if 'queued' in states else
                 'unknown' if 'unknown' in states else 'succeeded' if states and all(s == 'succeeded' for s in states)
                 else 'failed')
        db.execute('UPDATE env_jobs SET state=? WHERE job_id=? AND state != ?', (state, job_id, state))

    def _finish(self, job_id, env_id, state, result):
        store = self.store
        with store._lock:
            db = store._conn
            db.execute('BEGIN IMMEDIATE')
            try:
                cur = db.execute("UPDATE env_job_results SET state=?, result=? WHERE job_id=? AND env_id=? AND state IN ('running','unknown')",
                                 (state, json.dumps(result), job_id, env_id))
                if cur.rowcount:
                    self._aggregate(db, job_id)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def _claim(self, job_id, env_id, cap):
        store = self.store
        with store._lock:
            db = store._conn
            db.execute('BEGIN IMMEDIATE')
            try:
                used = db.execute("SELECT COUNT(*) FROM env_job_results WHERE env_id=? AND state IN ('running','unknown')", (env_id,)).fetchone()[0]
                claimed = False
                if type(cap) is int and cap > used:
                    claimed = bool(db.execute("UPDATE env_job_results SET state='running' WHERE job_id=? AND env_id=? AND state='queued'", (job_id, env_id)).rowcount)
                    if claimed:
                        self._aggregate(db, job_id)
                db.execute('COMMIT')
                return claimed
            except BaseException:
                db.execute('ROLLBACK')
                raise

    async def _execute(self, env, spec):
        job_id, env_id = spec['job_id'], env['id']
        try:
            if self.executor is None:
                self._finish(job_id, env_id, 'failed', {'status': 'error', 'error': 'executor_unavailable'})
                return
            submit = getattr(self.executor, 'submit', self.executor)
            result = await asyncio.to_thread(submit, env, spec)
            if not isinstance(result, dict) or result.get('status') not in ('ok', 'error'):
                raise ValueError('execution result unavailable')
            json.dumps(result)
            self._finish(job_id, env_id, 'succeeded' if result['status'] == 'ok' else 'failed', result)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._finish(job_id, env_id, 'unknown', {'status': 'error', 'error': 'execution_unknown'})

    async def _recover(self, *, unknown_only=False):
        state = 'unknown' if unknown_only else 'running'
        with self.store._lock:
            rows = self.store._conn.execute("SELECT r.job_id, r.env_id, j.spec FROM env_job_results r JOIN env_jobs j ON j.job_id=r.job_id WHERE r.state=?", (state,)).fetchall()
        try:
            envs = {e['id']: e for e in self.environments()}
        except Exception:
            envs = {}
        for row in rows:
            job_id, env_id = row['job_id'], row['env_id']
            try:
                status = getattr(self.executor, 'status', None)
                if status is None or env_id not in envs:
                    raise ValueError('status unavailable')
                result = await asyncio.to_thread(status, envs[env_id], json.loads(row['spec']))
                if not isinstance(result, dict) or result.get('status') not in ('ok', 'error'):
                    raise ValueError('status unavailable')
                json.dumps(result)
                self._finish(job_id, env_id, 'succeeded' if result['status'] == 'ok' else 'failed', result)
            except Exception:
                if not unknown_only:
                    self._finish(job_id, env_id, 'unknown', {'status': 'error', 'error': 'status_unknown'})

    async def _schedule(self):
        await self._recover()
        next_reconcile = time.monotonic() + self.reconcile_interval
        while True:
            try:
                if time.monotonic() >= next_reconcile:
                    if self._reconcile_task is None or self._reconcile_task.done():
                        self._reconcile_task = asyncio.create_task(self._recover(unknown_only=True))
                    next_reconcile = time.monotonic() + self.reconcile_interval
                envs = {e['id']: e for e in self.environments()}
                with self.store._lock:
                    rows = self.store._conn.execute("SELECT r.job_id, r.env_id, j.spec FROM env_job_results r JOIN env_jobs j ON j.job_id=r.job_id WHERE r.state='queued' ORDER BY r.rowid").fetchall()
                for row in rows:
                    env = envs.get(row['env_id'])
                    spec = json.loads(row['spec'])
                    if env is None or (env.get('state') != 'available' and not
                                       (env.get('state') == 'trial' and spec.get('trial_for') == env['id'])):
                        continue
                    if self._claim(row['job_id'], row['env_id'], env.get('max_concurrent')):
                        task = asyncio.create_task(self._execute(env, spec))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
            except Exception:
                pass
            await asyncio.sleep(0.1)

    async def start(self):
        self._runner = asyncio.create_task(self._schedule())

    async def stop(self):
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        if self._reconcile_task:
            self._reconcile_task.cancel()
            await asyncio.gather(self._reconcile_task, return_exceptions=True)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def post(self, request):
        owner = self.identify(request)
        if not owner:
            return json_error(401, 'unauthenticated', 'credential required')
        body, err = await read_json(request)
        if err:
            return err
        try:
            if not isinstance(body, dict):
                raise ValueError()
            job = EnvironmentJob.from_mapping(body)
            if not _ID.fullmatch(job.job_id):
                raise ValueError()
        except (EnvironmentContractError, ValueError, TypeError):
            return json_error(422, 'bad_request', 'invalid job')
        if job.entry.get('name') == 'hibi-state':
            from .adapters.ssh_host import HibiJobExecutor
            try:
                HibiJobExecutor._validate(body)
            except Exception:
                return json_error(422, 'bad_request', 'invalid Hibi job')
        if not self.permitted(owner, 'submit', job):
            return json_error(403, 'forbidden', 'operation not permitted')
        try:
            environments = list(self.environments())
            # Capacity is reserved transactionally after selection; busy candidates must remain queued.
            candidates = [{**e, 'slot_free': True} for e in environments]
            selected = select(candidates, SelectionRequest.from_job(job))
        except Exception:
            return json_error(503, 'environment_unavailable', 'environment selection unavailable')
        ids = [x['env_id'] for x in selected.candidates]
        if job.scope == 'one':
            ids = ids[:1]
        by_id = {e['id']: e for e in environments}
        store = self.store
        with store._lock:
            db = store._conn
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute("INSERT INTO env_jobs VALUES (?, ?, ?, 'queued')", (job.job_id, owner, json.dumps(body)))
                for env_id in ids:
                    entries = by_id[env_id].get('entrypoints', [])
                    name = job.entry.get('name')
                    allowed = name in entries and (not isinstance(entries, dict)
                        or not isinstance(entries[name], dict) or entries[name].get('enabled') is not False)
                    result = None if allowed else json.dumps({'status': 'error', 'error': 'entrypoint_unavailable'})
                    db.execute('INSERT INTO env_job_results VALUES (?, ?, ?, ?)', (job.job_id, env_id, 'queued' if allowed else 'failed', result))
                self._aggregate(db, job.job_id)
                db.execute('COMMIT')
            except sqlite3.IntegrityError:
                db.execute('ROLLBACK')
                return json_error(409, 'job_exists', 'job already exists')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return JSONResponse({'job_id': job.job_id, 'state': 'queued'}, status_code=202)

    async def get(self, request):
        owner = self.identify(request)
        if not owner:
            return json_error(401, 'unauthenticated', 'credential required')
        if not self.permitted(owner, 'get'):
            return json_error(403, 'forbidden', 'operation not permitted')
        with self.store._lock:
            db = self.store._conn
            job = db.execute('SELECT owner, state FROM env_jobs WHERE job_id=?', (request.path_params['job_id'],)).fetchone()
            if job is None or job['owner'] != owner:
                return json_error(404, 'not_found', 'job not found')
            rows = db.execute('SELECT env_id, state, result FROM env_job_results WHERE job_id=?', (request.path_params['job_id'],)).fetchall()
            results = {row['env_id']: json.loads(row['result']) if row['result'] else {'status': row['state']} for row in rows}
            response = {'job_id': request.path_params['job_id'], 'state': job['state'], 'results': results}
        return JSONResponse(response)

    async def artifact(self, request):
        from axcommon.defaults import Defaults
        owner = self.identify(request)
        if not owner:
            return json_error(401, 'unauthenticated', 'credential required')
        if not self.permitted(owner, 'get'):
            return json_error(403, 'forbidden', 'operation not permitted')
        job_id = request.path_params['job_id']
        if not _ID.fullmatch(job_id):
            return json_error(404, 'not_found', 'job not found')
        with self.store._lock:
            db = self.store._conn
            job = db.execute('SELECT owner, state, spec FROM env_jobs WHERE job_id=?', (job_id,)).fetchone()
            if job is None or job['owner'] != owner or job['state'] != 'succeeded':
                return json_error(404, 'not_found', 'artifact not found')
            spec = json.loads(job['spec'])
            if 'state.json' not in spec.get('outputs', []):
                return json_error(404, 'not_found', 'artifact not found')
            rows = db.execute('SELECT result FROM env_job_results WHERE job_id=?', (job_id,)).fetchall()
        if len(rows) != 1 or not rows[0]['result']:
            return json_error(404, 'not_found', 'artifact not found')
        result = json.loads(rows[0]['result'])
        data = result.get('artifacts', {}).get('state.json')
        if data is None:
            return json_error(404, 'not_found', 'artifact not found')
        response = JSONResponse(data)
        if len(response.body) > int(Defaults.load().get('hibi_body_max_kib')) * 1024:
            return json_error(413, 'too_large', 'artifact too large')
        return response

    def routes(self):
        return [Route('/v1/env/jobs', self.post, methods=['POST']),
                Route('/v1/env/jobs/{job_id}/artifacts/state.json', self.artifact, methods=['GET']),
                Route('/v1/env/jobs/{job_id}', self.get, methods=['GET'])]

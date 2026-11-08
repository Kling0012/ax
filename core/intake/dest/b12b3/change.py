"""Authenticated change requests backed by a durable compare-and-set ledger."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager

from axcommon import schemas


class ChangeError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


class ChangeLedger:
    def __init__(self, path, targets, *, resolve_pr=None):
        self.resolve_pr = resolve_pr
        self.path = str(path)
        self.targets = {x.lower() for x in targets}
        with self._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS change_requests (
                request_id TEXT PRIMARY KEY, issue_repo TEXT NOT NULL, kind TEXT NOT NULL,
                number INTEGER NOT NULL, owner TEXT NOT NULL, dry_run INTEGER NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', pr_number INTEGER, head_sha TEXT,
                UNIQUE(issue_repo, kind, number));
                CREATE TABLE IF NOT EXISTS change_events (
                    id INTEGER PRIMARY KEY, request_id TEXT, action TEXT NOT NULL,
                    principal TEXT NOT NULL, reason TEXT NOT NULL);''')

            columns = {row[1] for row in db.execute('PRAGMA table_info(change_requests)')}
            for column, kind in (('pr_number', 'INTEGER'), ('head_sha', 'TEXT')):
                if column not in columns:
                    db.execute(f'ALTER TABLE change_requests ADD COLUMN {column} {kind}')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def request(self, request_id, payload, principal):
        repo = payload['issue_repo'].lower()
        if not re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', repo) or repo not in self.targets:
            raise ChangeError('target_not_allowed', 404)
        if payload['kind'] not in ('issue', 'pr'):
            raise ChangeError('invalid_kind', 422)
        values = (repo, payload['kind'], payload['number'], principal, int(payload['dry_run']))
        # An issue has no independently established PR mapping at intake.
        if payload['kind'] != 'pr':
            raise ChangeError('pr_mapping_unavailable', 503)
        try:
            observed = self.resolve_pr(repo, payload['number']) if self.resolve_pr else None
        except Exception:
            observed = None
        if (not isinstance(observed, dict) or observed.get('issue_repo', '').lower() != repo
                or type(observed.get('number')) is not int or observed['number'] != payload['number']
                or not isinstance(observed.get('head_sha'), str)
                or not re.fullmatch(r'[0-9a-fA-F]{40}', observed['head_sha'])):
            raise ChangeError('pr_state_unavailable', 503)
        pr_number, head_sha = observed['number'], observed['head_sha'].lower()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM change_requests WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if tuple(old[k] for k in ('issue_repo', 'kind', 'number', 'owner', 'dry_run')) != values:
                    raise ChangeError('request_id_conflict')
                return dict(old)
            try:
                db.execute('INSERT INTO change_requests(request_id,issue_repo,kind,number,owner,dry_run,state,pr_number,head_sha) VALUES(?,?,?,?,?,?,?,?,?)',
                           (request_id, *values, 'waiting', pr_number, head_sha))
            except sqlite3.IntegrityError as exc:
                raise ChangeError('target_already_requested') from exc
            db.execute('INSERT INTO change_events(request_id,action,principal,reason) VALUES(?,?,?,?)',
                       (request_id, 'request', principal, ''))
            return dict(db.execute('SELECT * FROM change_requests WHERE request_id=?', (request_id,)).fetchone())

    def for_item(self, item):
        repo, kind, number = item["issue_repo"].lower(), item["kind"], item["number"]
        if repo not in self.targets or kind not in ("issue", "pr") or type(number) is not int or number < 1:
            return None
        request_id = item.get("request_id")
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_id:
                row = db.execute('SELECT * FROM change_requests WHERE request_id=?', (request_id,)).fetchone()
            else:
                row = db.execute('SELECT * FROM change_requests WHERE issue_repo=? AND kind=? AND number=?', (repo, kind, number)).fetchone()
            return dict(row) if row else None

    def cancel(self, payload, principal):
        repo, number, reason = payload['issue_repo'].lower(), payload['number'], payload['reason'].strip()
        if not reason:
            raise ChangeError('reason_required', 422)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM change_requests WHERE issue_repo=? AND kind='pr' AND number=?", (repo, number)).fetchone()
            if not row or repo not in self.targets:
                code, status = 'target_not_found', 404
            elif row['owner'] != principal:
                code, status = 'not_owner', 403
            elif row['state'] != 'waiting':
                code, status = 'too_late_' + row['state'], 409
            else:
                try:
                    observed = self.resolve_pr(repo, number) if self.resolve_pr else None
                except Exception:
                    observed = None
                if (not isinstance(observed, dict) or observed.get('issue_repo', '').lower() != repo
                        or observed.get('number') != number or type(observed.get('merged')) is not bool
                        or type(observed.get('untrusted')) is not bool):
                    code, status = 'pr_state_unavailable', 503
                elif observed['merged']:
                    db.execute("UPDATE change_requests SET state='merged',reason='already_merged' WHERE request_id=? AND state='waiting'", (row['request_id'],))
                    code, status = 'too_late_merged', 409
                elif not observed['untrusted']:
                    code, status = 'not_untrusted', 403
                else:
                    changed = db.execute("UPDATE change_requests SET state='cancelled',reason=? WHERE request_id=? AND owner=? AND state='waiting'", (reason, row['request_id'], principal)).rowcount
                    code, status = ('cancelled', 200) if changed else ('too_late', 409)
            db.execute('INSERT INTO change_events(request_id,action,principal,reason) VALUES(?,?,?,?)',
                       (row['request_id'] if row else None, 'cancel', principal, code + ': ' + reason))
        if status != 200:
            raise ChangeError(code, status)
        return {'request_id': row['request_id'], 'state': 'cancelled'}

    def transition(self, request_id, before, after, reason='', *, pr_number=None, head_sha=None):
        allowed = {('waiting', 'merging'), ('merging', 'merged'), ('merging', 'waiting'), ('merging', 'unknown')}
        if (before, after) not in allowed:
            raise ValueError('invalid transition')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if before == 'waiting' and after == 'merging':
                changed = db.execute('UPDATE change_requests SET state=?,reason=? WHERE request_id=? AND state=? AND dry_run=0 AND pr_number=? AND head_sha=?',
                                     (after, reason, request_id, before, pr_number, head_sha)).rowcount
            else:
                changed = db.execute('UPDATE change_requests SET state=?,reason=? WHERE request_id=? AND state=? AND dry_run=0',
                                     (after, reason, request_id, before)).rowcount
            if changed:
                db.execute('INSERT INTO change_events(request_id,action,principal,reason) VALUES(?,?,?,?)',
                           (request_id, after, 'change', reason))
            return changed == 1

    def get(self, request_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM change_requests WHERE request_id=?', (request_id,)).fetchone()
            return dict(row) if row else None


def register(app):
    for name in ('change.request', 'change.cancel_merge'):
        app.state.intake.dest_handlers[name] = handle


def handle(app, dest, body, ident):
    name = body.get('destination')
    if name not in ('change.request', 'change.cancel_merge'):
        return 404, {'error': {'code': 'unknown_destination'}}
    if (ident.from_ not in ('commander', 'hq') or not ident.principal or
        (ident.via == 'hq') != (ident.from_ == 'hq') or
        (ident.via == 'hq' and ident.principal != 'user') or
        ident.via not in ('local', 'ssh', 'tailnet', 'hq') or ident.max_tier not in ('operate', 'irreversible')):
        return 403, {'error': {'code': 'forged_authority'}}
    payload = body.get('payload')
    if schemas.errors('dest/' + name + '.json', payload):
        return 422, {'error': {'code': 'payload_schema'}}
    ledger = getattr(app.state, 'change_ledger', None)
    if ledger is None:
        return 503, {'error': {'code': 'change_unavailable'}}
    try:
        if name == 'change.request':
            key = body.get('idempotency_key')
            if not isinstance(key, str) or not key.strip():
                return 422, {'error': {'code': 'idempotency_key_required'}}
            result = ledger.request(key, payload, ident.principal)
        else:
            result = ledger.cancel(payload, ident.principal)
        return 200, result
    except ChangeError as exc:
        return exc.status, {'error': {'code': exc.code}}
    except (sqlite3.Error, OSError):
        return 503, {'error': {'code': 'change_unavailable'}}

"""Durable queue, immutable batches, operation receipts and timeline outbox."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import re
import sqlite3
import threading

from axcommon.events import build_event
from axcommon.ids import new_ulid
from axcommon.timeutil import now_ts, parse_ts
from .registry import Held, target_changed


class Store:
    def __init__(self, path: str | Path, *, clock=now_ts, scrub=None, host='unknown'):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.mutex = threading.RLock()
        self.clock, self.scrub, self.host = clock, scrub or (lambda value: value), host
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS items(repo TEXT, pr INTEGER, data TEXT NOT NULL, PRIMARY KEY(repo,pr));
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS locks(repo TEXT, mode TEXT, batch TEXT, PRIMARY KEY(repo,mode));
        CREATE TABLE IF NOT EXISTS ops(key TEXT PRIMARY KEY, batch TEXT, state TEXT, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, destination TEXT NOT NULL, payload TEXT NOT NULL, receipt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, body TEXT NOT NULL, sent INTEGER DEFAULT 0);
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def tx(self):
        with self.mutex:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            else:
                self.db.execute('COMMIT')

    def event(self, kind, subject, data):
        event = build_event(component='release', host=self.host, kind=kind, subject=subject,
                            subloop='release', data=self.scrub(deepcopy(data)))
        self.db.execute('INSERT INTO events(body) VALUES (?)', (json.dumps(event, ensure_ascii=False),))

    def _put_item(self, item):
        self.db.execute('INSERT OR REPLACE INTO items VALUES (?,?,?)',
                        (item['repo'], item['pr'], json.dumps(item, ensure_ascii=False)))

    def _put_batch(self, batch):
        self.db.execute('INSERT OR REPLACE INTO batches VALUES (?,?)', (batch['batch_id'], json.dumps(batch)))

    def item(self, repo, pr):
        row = self.db.execute('SELECT data FROM items WHERE repo=? AND pr=?', (repo, pr)).fetchone()
        return json.loads(row['data']) if row else None

    def items(self):
        return [json.loads(row['data']) for row in self.db.execute('SELECT data FROM items ORDER BY repo,pr')]

    def batch(self, batch_id):
        row = self.db.execute('SELECT data FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise Held('batch_unknown')
        return json.loads(row['data'])

    def batches(self):
        return [json.loads(row['data']) for row in self.db.execute('SELECT data FROM batches ORDER BY id')]

    def put(self, key, value):
        with self.tx():
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))

    def get(self, key, default=None):
        row = self.db.execute('SELECT data FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row['data']) if row else default

    def ingest(self, product, *, repo, pr, merge_sha, merged_at, issues, changed_paths):
        with self.tx():
            return self._ingest(product, repo=repo, pr=pr, merge_sha=merge_sha, merged_at=merged_at,
                                issues=issues, changed_paths=changed_paths)

    def _ingest(self, product, *, repo, pr, merge_sha, merged_at, issues, changed_paths):
        if repo != product['repo'] or type(pr) is not int or pr < 1 or not isinstance(merge_sha, str) or not re.fullmatch('[0-9a-f]{40}', merge_sha):
            raise Held('invalid_merge')
        parse_ts(merged_at)
        previous = self.item(repo, pr)
        if previous is not None:
            return previous
        try:
            changed = target_changed(product, changed_paths)
            state = 'pending' if changed else 'skipped'
            reason = 'merged' if changed else 'paths_outside_product'
        except Held:
            state, reason = 'held', 'changed_paths_unavailable'
        if state == 'pending' and not product.get('enabled', True):
            state, reason = 'disabled', 'automatic_release_disabled'
        clean_issues = []
        for issue in issues:
            if type(issue.get('number')) is not int or issue['number'] < 1:
                raise Held('invalid_issue')
            clean_issue = {'number': issue['number'], 'diagnostic': issue.get('diagnostic', False) is True,
                           'log_wait': issue.get('log_wait', False) is True}
            if 'issue_repo' in issue:
                issue_repo = issue['issue_repo']
                if not isinstance(issue_repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', issue_repo):
                    raise Held('invalid_issue_repo')
                clean_issue['issue_repo'] = issue_repo
            clean_issues.append(clean_issue)
        item = dict(repo=repo, pr=pr, merge_sha=merge_sha, merged_at=merged_at,
                    issues=clean_issues, state=state, batch_id=None,
                    product_id=product['id'], reason=reason, changed_paths=changed_paths)
        self._put_item(item)
        self.event('release.pending', f'{repo}#{pr}', item)
        if state in ('skipped', 'disabled'):
            self._issue_actions([item], state, None)
        return deepcopy(item)


    def deliver(self, product, delivery_id, destination, payload):
        if not isinstance(delivery_id, str) or not delivery_id or len(delivery_id) > 512 or destination != 'release' or not isinstance(payload, dict):
            raise Held('invalid_delivery')
        required = {'repo', 'pr', 'merge_sha', 'merged_at', 'issues', 'changed_paths'}
        if (set(payload) != required or not isinstance(payload['issues'], list)
                or not all(isinstance(issue, dict) for issue in payload['issues'])
                or not isinstance(payload['changed_paths'], list)
                or not all(isinstance(path, str) and path for path in payload['changed_paths'])):
            raise Held('invalid_delivery_payload')
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        with self.tx():
            row = self.db.execute('SELECT destination,payload,receipt FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if row:
                if row['destination'] != destination or row['payload'] != encoded:
                    raise Held('delivery_identity_conflict')
                return json.loads(row['receipt'])
            previous = self.item(payload['repo'], payload['pr'])
            if previous is not None:
                normalized_issues = []
                for issue in payload['issues']:
                    if not isinstance(issue, dict):
                        raise Held('invalid_issue')
                    clean = {'number': issue.get('number'), 'diagnostic': issue.get('diagnostic', False) is True,
                             'log_wait': issue.get('log_wait', False) is True}
                    if 'issue_repo' in issue:
                        clean['issue_repo'] = issue['issue_repo']
                    normalized_issues.append(clean)
                if any(previous.get(field) != payload[field] for field in ('repo', 'pr', 'merge_sha', 'merged_at', 'changed_paths')) or previous.get('issues') != normalized_issues:
                    raise Held('release_item_conflict')
            item = self._ingest(product, **payload)
            receipt = {'delivery_id': delivery_id, 'destination': destination, 'status': 'delivered',
                       'delivered_at': parse_ts(self.clock()).timestamp(), 'repo': item['repo'], 'pr': item['pr'],
                       'merge_sha': item['merge_sha'], 'merged_at': item['merged_at']}
            self.db.execute('INSERT INTO deliveries VALUES (?,?,?,?)', (delivery_id, destination, encoded, json.dumps(receipt)))
            return receipt

    def lookup_delivery(self, delivery_id, destination):
        if not isinstance(delivery_id, str) or not delivery_id or destination != 'release':
            raise Held('invalid_delivery')
        with self.mutex:
            row = self.db.execute('SELECT destination,receipt FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if row is None or row['destination'] != destination:
                return {'status': 'unknown'}
            return json.loads(row['receipt'])

    def _issue_actions(self, items, outcome, batch_id):
        # Durable intents; an adapter must acknowledge each key after reconciliation.
        for item in items:
            for issue in item['issues']:
                issue_repo = issue.get('issue_repo', item['repo'])
                key = f"issue:{item['repo']}:{item['pr']}:{issue['number']}:{batch_id or outcome}"
                if issue_repo.lower() != item['repo'].lower():
                    key += f":{issue_repo.lower()}"
                action = {'key': key, 'repo': issue_repo, 'issue': issue['number'], 'outcome': outcome,
                          'close': outcome in ('success', 'skipped', 'disabled') and not (issue['diagnostic'] or issue['log_wait']),
                          'published': outcome == 'success', 'batch_id': batch_id}
                self.db.execute('INSERT OR IGNORE INTO kv VALUES (?,?)', (key, json.dumps(action)))

    def form_batch(self, product, target_sha, mode='release', *, force=False):
        if not re.fullmatch('[0-9a-f]{40}', target_sha):
            raise Held('invalid_target_sha')
        with self.tx():
            items = [i for i in self.items() if i['repo'] == product['repo'] and i['state'] == 'pending' and i['batch_id'] is None]
            if not items:
                return None
            first = min(parse_ts(i['merged_at']) for i in items)
            last = max(parse_ts(i['merged_at']) for i in items)
            due = min(last + timedelta(minutes=product.get('quiet_min', 20)),
                      first + timedelta(minutes=product.get('max_wait_min', 90)))
            if not force and parse_ts(self.clock()) < due:
                return None
            batch = {'batch_id': new_ulid(), 'repo': product['repo'], 'product_id': product['id'], 'mode': mode,
                     'target_sha': target_sha, 'prs': sorted(i['pr'] for i in items), 'state': 'ready',
                     'created_at': self.clock(), 'lock_started_at': None}
            for item in items:
                item.update(state='in_batch', batch_id=batch['batch_id'])
                self._put_item(item)
            self._put_batch(batch)
            self.event('release.batch', batch['batch_id'], batch)
            return deepcopy(batch)

    def acquire(self, batch_id, *, wait_min=40):
        with self.tx():
            b = self.batch(batch_id)
            held = self.db.execute('SELECT batch FROM locks WHERE repo=? AND mode=?', (b['repo'], b['mode'])).fetchone()
            if not held or held['batch'] == batch_id:
                self.db.execute('INSERT OR REPLACE INTO locks VALUES (?,?,?)', (b['repo'], b['mode'], batch_id))
                return True
            b['lock_started_at'] = b.get('lock_started_at') or self.clock()
            elapsed = (parse_ts(self.clock()) - parse_ts(b['lock_started_at'])).total_seconds()
            if elapsed >= wait_min * 60:
                b.update(state='held', reason='release_lock_wait_exceeded')
                for pr in b['prs']:
                    item = self.item(b['repo'], pr)
                    item.update(state='held', reason=b['reason'])
                    self._put_item(item)
                self.event('release.held', batch_id, {'reason': b['reason']})
            self._put_batch(b)
            return False

    def unlock(self, batch_id):
        with self.tx():
            self.db.execute('DELETE FROM locks WHERE batch=?', (batch_id,))

    def operation(self, key):
        row = self.db.execute('SELECT data FROM ops WHERE key=?', (key,)).fetchone()
        return json.loads(row['data']) if row else None

    def launching(self, key, batch_id, scope):
        with self.tx():
            if self.operation(key):
                return False
            data = {'key': key, 'batch_id': batch_id, 'state': 'launching', 'scope': scope, 'at': self.clock()}
            self.db.execute('INSERT INTO ops VALUES (?,?,?,?)', (key, batch_id, 'launching', json.dumps(data)))
            self.event('run.state', batch_id, {'state': 'launching', 'reason': 'release_operation', 'operation_ref': key})
            return True

    def finish_operation(self, key, state, result):
        with self.tx():
            data = self.operation(key)
            if data is None:
                raise Held('operation_not_launched')
            data.update(state=state, result=self.scrub(result), at=self.clock())
            self.db.execute('UPDATE ops SET state=?,data=? WHERE key=?', (state, json.dumps(data), key))
            self.event('run.state', data['batch_id'], {'state': state, 'reason': 'release_operation', 'operation_ref': key})

    def result(self, batch_id, *, outcome, reason, version=None, evidence_refs=(), dry=False, retry_limit=3):
        if outcome not in ('success', 'failed', 'held', 'skipped', 'disabled'):
            raise Held('invalid_release_outcome')
        with self.tx():
            b = self.batch(batch_id)
            count_key = f'retries:{batch_id}:{reason}'
            count = self.get(count_key, 0) + (outcome in ('failed', 'held'))
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (count_key, json.dumps(count)))
            if count >= retry_limit and outcome == 'failed':
                outcome = 'held'
            result = {'batch_id': batch_id, 'repo': b['repo'], 'mode': b['mode'], 'target_sha': b['target_sha'],
                      'version': version, 'outcome': outcome, 'reason': reason,
                      'evidence_refs': list(evidence_refs), 'at': self.clock()}
            b['last_result'] = result
            b['dry_run'] = dry
            items = [self.item(b['repo'], pr) for pr in b['prs']]
            if dry:
                b['state'] = 'dry_run'
                for item in items:
                    item.update(state='pending', batch_id=None, reason='dry_run')
                    self._put_item(item)
            else:
                b['state'] = outcome
                state = {'success': 'released', 'failed': 'held'}.get(outcome, outcome)
                for item in items:
                    item.update(state=state, reason=reason)
                    self._put_item(item)
                self._issue_actions(items, outcome, batch_id)
            self._put_batch(b)
            self.event('release.result', batch_id, result)
            if outcome in ('failed', 'held', 'disabled') and not dry:
                self.event('release.card', batch_id, {'outcome': outcome, 'reason': reason, 'next_check': 'verify_release_evidence'})
            return deepcopy(result)

    def replay(self, events):
        """Recover queue and batches from S-20 timeline records, preserving terminal states."""
        with self.tx():
            for event in events:
                kind, data = event['kind'], event['data']
                if kind == 'release.pending':
                    self._put_item(data)
                elif kind == 'release.batch':
                    self._put_batch(data)
                    for pr in data['prs']:
                        item = self.item(data['repo'], pr)
                        if item:
                            item.update(state='in_batch', batch_id=data['batch_id'])
                            self._put_item(item)
                elif kind == 'release.result':
                    b = self.batch(data['batch_id'])
                    # dry-run records have no terminal effects; discriminate by reason.
                    b['last_result'] = data
                    if data['reason'] != 'dry_run':
                        b['state'] = data['outcome']
                        for pr in b['prs']:
                            item = self.item(b['repo'], pr)
                            item.update(state={'success': 'released', 'failed': 'held'}.get(data['outcome'], data['outcome']))
                            self._put_item(item)
                    self._put_batch(b)

    def flush(self, send):
        for row in self.db.execute('SELECT id,body FROM events WHERE sent=0 ORDER BY id').fetchall():
            try:
                send(json.loads(row['body']))
            except Exception:
                break
            self.db.execute('UPDATE events SET sent=1 WHERE id=?', (row['id'],))

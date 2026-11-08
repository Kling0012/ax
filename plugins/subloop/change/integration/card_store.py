"""Append-only scoped cards and HQ signed answers for change integration.

The database must be writable only by the trusted HQ/card service. The change
process may read it; signed answers are verified even if storage is tampered.
"""
import base64
import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from axcommon import hq_proof

_PATH = '/internal/change-card/answer'


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result


class CardStore:
    def __init__(self, database, ledger, *, clock=None):
        self.db = sqlite3.connect(database)
        self.ledger = ledger
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS change_cards (id TEXT PRIMARY KEY, scope TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS change_answers (card_id TEXT PRIMARY KEY, body TEXT NOT NULL, proof TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS change_cards_no_update BEFORE UPDATE ON change_cards BEGIN SELECT RAISE(ABORT, 'append only'); END;
        CREATE TRIGGER IF NOT EXISTS change_cards_no_delete BEFORE DELETE ON change_cards BEGIN SELECT RAISE(ABORT, 'append only'); END;
        CREATE TRIGGER IF NOT EXISTS change_answers_no_update BEFORE UPDATE ON change_answers BEGIN SELECT RAISE(ABORT, 'append only'); END;
        CREATE TRIGGER IF NOT EXISTS change_answers_no_delete BEFORE DELETE ON change_answers BEGIN SELECT RAISE(ABORT, 'append only'); END;
        ''')

    def wait(self, item, *, repo, pr, head_sha, reason):
        from datetime import timedelta
        operation = {'publication_permission_missing': 'pr_update', 'comment_permission_missing': 'comment'}.get(reason, 'merge')
        scope = {'repo': repo, 'pr': pr, 'head_sha': head_sha, 'operation': operation}
        encoded = _canonical(scope).decode()
        now = self.clock()
        for card_id, expiry in self.db.execute('SELECT id, expires_at FROM change_cards WHERE scope=? ORDER BY rowid DESC', (encoded,)):
            if _time(expiry) > now:
                return card_id
        card_id = uuid4().hex
        expiry = (now + timedelta(hours=1)).isoformat()
        # Register the ledger card before exposing the scoped card to HQ.
        self.ledger.create_todo({'id': card_id, 'title': 'Change permission', 'state': 'waiting_user',
            'owner': 'system', 'user_only': True, 'card': {'question': f'Approve {operation} for {repo} PR {pr} at {head_sha}?',
            'waiting_reason': 'irreversible', 'user_only': True}}, actor='change')
        with self.db:
            self.db.execute('INSERT INTO change_cards VALUES (?, ?, ?)', (card_id, encoded, expiry))
        return card_id

    def verified_answer(self, scope):
        encoded = _canonical(scope).decode()
        rows = self.db.execute('SELECT c.id, c.scope, c.expires_at, a.body, a.proof FROM change_cards c JOIN change_answers a ON a.card_id=c.id WHERE c.scope=? ORDER BY a.rowid DESC', (encoded,))
        for card_id, raw_scope, expiry, raw_body, token in rows:
            try:
                body = json.loads(raw_body)
                payload, signature = token.split('.')
                expected = hmac.new(hq_proof._key(), payload.encode('ascii'), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    continue
                claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
                if (claims.get('method') != 'POST' or claims.get('path') != _PATH
                    or claims.get('body') != hashlib.sha256(_canonical(body)).hexdigest()
                    or claims.get('sub') != body.get('principal') or claims.get('factor') != body.get('second_factor')
                    or type(claims.get('iat')) is not int or claims.get('exp') != claims['iat'] + 60
                    or body.get('card_id') != card_id or body.get('scope') != scope
                    or body.get('expires_at') != expiry or body.get('auth_method') != 'hq_tailnet'
                    or not isinstance(body.get('principal'), str) or not body['principal']
                    or type(body.get('second_factor')) is not bool
                    or abs(_time(body['answered_at']).timestamp() - claims['iat']) > 2
                    or _time(body['answered_at']) > self.clock() or _time(expiry) <= self.clock()):
                    continue
                return {**scope, 'authenticated': True, 'decision': body['decision'], 'active': True,
                        'expires_at': expiry, 'card_id': card_id, 'instruction_ref': card_id}
            except (ValueError, TypeError, KeyError, UnicodeError):
                continue
        return None

"""Short-lived, request-bound HQ assertions over the authenticated HQ socket."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

HEADER = b'x-ax-hq-proof'


class KeyUnavailable(ValueError):
    pass


def requires_proof(destination):
    return (isinstance(destination, str) and
            (destination in {'approval.grant', 'approval.deny'}
             or destination.startswith(('permission.', 'seat.'))))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _key():
    path = os.environ.get('AX_HQ_PROOF_KEY_FILE', '/run/secrets/hq-proof-key')
    try:
        key = Path(path).read_bytes()
    except OSError:
        raise KeyUnavailable('proof_key_unavailable') from None
    if len(key) < 32:
        raise KeyUnavailable('proof_key_unavailable')
    return key


def encode(method, path, body, verified):
    from core.hq.approval.security import VerifiedAction
    if not isinstance(verified, VerifiedAction):
        raise ValueError('unauthorized')
    who = verified.identity
    if not who.allowed or not who.direct_tailnet or not who.subject or not who.session:
        raise ValueError('unauthorized')
    now = int(time.time())
    claims = {'sub': who.subject, 'request_id': secrets.token_urlsafe(24), 'iat': now, 'exp': now + 60,
              'nonce': secrets.token_urlsafe(24), 'method': method, 'path': path,
              'body': hashlib.sha256(_json(body or {})).hexdigest(), 'factor': verified.second_factor}
    payload = base64.urlsafe_b64encode(_json(claims)).decode().rstrip('=')
    signature = hmac.new(_key(), payload.encode('ascii'), hashlib.sha256).hexdigest()
    return body, {'X-AX-HQ-Proof': payload + '.' + signature}


class Verifier:
    def __init__(self, database):
        self.lock = threading.Lock()
        fd = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(database, 0o600)
        self.db = sqlite3.connect(str(database), check_same_thread=False, isolation_level=None)
        self.db.execute('CREATE TABLE IF NOT EXISTS hq_nonces (nonce TEXT PRIMARY KEY, expires INTEGER NOT NULL)')

    def __call__(self, scope, body):
        from core.intake.approval.service import VerifiedUser
        from core.intake.approval.store import ApprovalError
        from core.intake.vias import Ident
        from dataclasses import replace
        cached = scope.get('hq.verified_proof')
        if cached is not None:
            return cached
        try:
            ident = scope.get('ax_ident')
            if not isinstance(ident, Ident) or (ident.via, ident.from_, ident.principal) != ('hq', 'hq', 'user'):
                raise ValueError()
            key = _key()
            headers = [v for k, v in scope.get('headers', []) if k.lower() == HEADER]
            if len(headers) != 1 or len(headers[0]) > 8192:
                raise ValueError()
            token = headers[0].decode('ascii')
            payload, signature = token.split('.')
            expected = hmac.new(key, payload.encode('ascii'), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            now = int(time.time())
            if (set(claims) != {'sub', 'request_id', 'iat', 'exp', 'nonce', 'method', 'path', 'body', 'factor'}
                or not all(isinstance(claims[k], str) and 0 < len(claims[k]) <= 256 for k in ('sub', 'request_id', 'nonce'))
                or type(claims['iat']) is not int or type(claims['exp']) is not int
                or type(claims['factor']) is not bool or claims['exp'] != claims['iat'] + 60
                or not claims['iat'] <= now < claims['exp']
                or claims['method'] != scope.get('method') or claims['path'] != scope.get('path')
                or not hmac.compare_digest(claims['body'], hashlib.sha256(_json(body or {})).hexdigest())):
                raise ValueError()
            with self.lock:
                self.db.execute('DELETE FROM hq_nonces WHERE expires <= ?', (now,))
                self.db.execute('INSERT INTO hq_nonces VALUES (?, ?)', (claims['nonce'], claims['exp']))
            result = VerifiedUser(True, claims['factor'], True, claims['sub'])
            scope['ax_ident'] = replace(ident, principal=claims['sub'])
            scope['hq.verified_proof'] = result
            return result
        except KeyUnavailable:
            raise ApprovalError(503, 'proof_key_unavailable') from None
        except (ValueError, TypeError, KeyError, UnicodeError, sqlite3.Error):
            raise ApprovalError(403, 'unauthorized') from None

"""Change approval scope, component proof and online HQ client."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from axcommon import hq_proof
from axcommon.client import ApiClient

DESTINATION = 'change.approval.record'
REQUEST_PATH = '/v1/change-approval/request'
CHECK_PATH = '/v1/change-approval/check'
INTERNAL_PATHS = frozenset({REQUEST_PATH, CHECK_PATH})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def validate_scope(value):
    if not isinstance(value, dict) or set(value) != {'project', 'item', 'repo', 'pr', 'head_sha', 'input_revision', 'operation', 'expires_at'}:
        raise ValueError('invalid_scope')
    item = value['item']
    if not isinstance(item, dict) or set(item) != {'provider', 'issue_repo', 'kind', 'number'}:
        raise ValueError('invalid_item')
    if item['provider'] != 'github' or item['kind'] not in {'issue', 'pr'} or type(item['number']) is not int or item['number'] <= 0:
        raise ValueError('invalid_item')
    for text in (value['project'], value['repo'], value['input_revision'], item['issue_repo']):
        if not isinstance(text, str) or not text.strip() or len(text) > 256:
            raise ValueError('invalid_scope')
    if not all(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) for repo in (value['repo'], item['issue_repo'])):
        raise ValueError('invalid_repo')
    op, pr, head = value['operation'], value['pr'], value['head_sha']
    if op not in {'merge', 'pr_update', 'comment'}:
        raise ValueError('invalid_operation')
    if op == 'merge' and (type(pr) is not int or pr <= 0):
        raise ValueError('invalid_pr')
    if op in {'pr_update', 'comment'} and pr is not None and (type(pr) is not int or pr <= 0):
        raise ValueError('invalid_pr')
    if op in {'merge', 'pr_update'} and (not isinstance(head, str) or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', head)):
        raise ValueError('invalid_head')
    if op == 'comment' and head is not None and (not isinstance(head, str) or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', head)):
        raise ValueError('invalid_head')
    expires = value['expires_at']
    if not isinstance(expires, str):
        raise ValueError('invalid_expiry')
    dt = datetime.fromisoformat(expires.replace('Z', '+00:00'))
    if dt.tzinfo is None or not math.isfinite(dt.timestamp()):
        raise ValueError('invalid_expiry')
    return value


def component_proof(path, body):
    if path not in INTERNAL_PATHS:
        raise ValueError('invalid_path')
    now = int(time.time())
    claims = {'purpose': DESTINATION, 'component': 'change', 'method': 'POST', 'path': path,
              'iat': now, 'body': hashlib.sha256(canonical(body)).hexdigest()}
    payload = canonical(claims).hex()
    mac = hmac.new(hq_proof._key(), payload.encode('ascii'), hashlib.sha256).hexdigest()
    return payload + '.' + mac


def verify_component_proof(path, body, token):
    if path not in INTERNAL_PATHS or not isinstance(token, str) or len(token) > 8192:
        return False
    try:
        payload, mac = token.split('.')
        expected = hmac.new(hq_proof._key(), payload.encode('ascii'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return False
        claims = json.loads(bytes.fromhex(payload))
        now = int(time.time())
        return (set(claims) == {'purpose', 'component', 'method', 'path', 'iat', 'body'}
                and claims['purpose'] == DESTINATION and claims['component'] == 'change'
                and claims['method'] == 'POST' and claims['path'] == path
                and type(claims['iat']) is int and claims['iat'] <= now < claims['iat'] + 60
                and hmac.compare_digest(claims['body'], hashlib.sha256(canonical(body)).hexdigest()))
    except (ValueError, TypeError, KeyError, UnicodeError):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


class ChangeApprovalClient:
    """Caller supplies the existing change component token, never a user proof."""
    def __init__(self, token, *, transport=None):
        if not isinstance(token, str) or not token:
            raise ValueError('component_credential_required')
        self._api = ApiClient('http://hq:8083', token, transport=transport or _transport)

    def request(self, request_id: str, scope: dict) -> dict:
        validate_scope(scope)
        return self._api.post('/v1/change/approval-requests', {'request_id': request_id, 'scope': scope})

    def check(self, approval_id: str, scope: dict) -> dict:
        validate_scope(scope)
        try:
            result = self._api.post('/v1/change/approval-check', {'approval_id': approval_id, 'scope': scope})
        except Exception:
            return {'status': 'unknown'}
        if not isinstance(result, dict) or result.get('approval_id') != approval_id or result.get('scope') != scope:
            return {'status': 'unknown'}
        if result.get('status') not in {'approved', 'pending', 'denied', 'expired', 'withdrawn', 'unknown'}:
            return {'status': 'unknown'}
        if result.get('status') == 'approved':
            expiry = datetime.fromisoformat(scope['expires_at'].replace('Z', '+00:00'))
            if expiry <= datetime.now(timezone.utc):
                return {'status': 'expired'}
        return result

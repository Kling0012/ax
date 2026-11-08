"""Offline ASGI caller and trusted intake adapter for HQ acceptance tests."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from html.parser import HTMLParser
import json

from core.hq.approval.service import ApprovalFailure
from core.hq.seats.service import RegistrationDisplay


class FakeFactorAdapters:
    def __init__(self):
        self.events = {}
        self.notifications = {}

    def emit(self, item):
        self.events.setdefault(item['id'], deepcopy(item))

    def notify(self, item):
        self.notifications.setdefault(item['id'], deepcopy(item))


class Forms(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.values = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('type') == 'hidden':
            self.values[attrs['name']] = attrs['value']


def call(app, method, path, *, body=None, headers=None, raw=None):
    values = {'host': 'hq.test', 'origin': 'https://hq.test'}
    values.update(headers or {})
    values = {k: v for k, v in values.items() if v is not None}
    if raw is None:
        raw = json.dumps(body).encode() if body is not None else b''
    if body is not None:
        values['content-type'] = 'application/json'
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
             'scheme': 'https', 'path': path.split('?')[0], 'raw_path': path.split('?')[0].encode(),
             'query_string': path.partition('?')[2].encode(), 'headers': [(k.encode(), v.encode()) for k, v in values.items()],
             'server': ('hq.test', 443), 'client': ('127.0.0.1', 1)}
    result = {'body': b''}
    async def receive():
        return {'type': 'http.request', 'body': raw, 'more_body': False}
    async def send(message):
        if message['type'] == 'http.response.start':
            result.update(status=message['status'], headers=dict(message['headers']))
        elif message['type'] == 'http.response.body':
            result['body'] += message.get('body', b'')
    asyncio.run(app(scope, receive, send))
    result['text'] = result['body'].decode()
    return result


class FakeIntake:
    def __init__(self):
        self.saved = {'approval_id': 'approval-a', 'destination': 'core.change', 'digest': 'a' * 64,
                      'requested_by': 'seat-a', 'caller': {'device': 'device-a', 'tier': 'irreversible'},
                      'expires_at': '2026-11-07T23:00:00Z', 'state': 'pending_user',
                      'payload': {'reason': '</pre><script>unsafe()</script>', 'target': 'module'}}
        self.calls, self.executed, self.seat_calls = [], [], []
        self.denied = False
        self.expired = False
        self.mismatch = False

    def approvals(self, proof):
        return {'approvals': [deepcopy(self.saved)]}

    def approval(self, aid, proof):
        if aid != self.saved['approval_id']:
            raise ApprovalFailure(404, 'approval_not_found')
        return {'data': deepcopy(self.saved)}

    def grant(self, aid, digest, proof):
        if not proof.second_factor or not proof.identity.direct_tailnet:
            raise ApprovalFailure(403, 'unauthorized')
        if self.saved['state'] != 'pending_user' or self.expired:
            raise ApprovalFailure(409, 'approval_not_pending')
        if digest != self.saved['digest']:
            raise ApprovalFailure(409, 'approval_mismatch')
        self.calls.append((aid, digest, proof))
        self.executed.append({'payload': deepcopy(self.saved['payload']), 'issued_by': 'user', 'approved_via': aid})
        self.saved['state'] = 'executed'
        return 200, {'ok': True}

    def deny(self, aid, digest, proof, *, request_id):
        self.denied = True
        self.saved['state'] = 'denied'
        return 200, {'ok': True}

    def seats(self, proof):
        return {'seats': [{'seat_id': 'seat-a', 'label': '<script>seat</script>', 'device': 'device-a',
                          'via': 'ssh', 'tier': 'read', 'state': 'active', 'key_fp': 'SHA256:fake',
                          'last_seen_at': '2026-11-07T15:00:00Z', 'expires_at': '2026-12-01T00:00:00Z'}],
                'summary': {'recent_denials': [{'kind': 'auth.failed'}], 'lockdown': False, 'seat.lock': []}}

    def submit(self, destination, payload, proof, *, request_id):
        self.seat_calls.append((destination, deepcopy(payload), proof, request_id))
        if destination == 'seat.enroll_start':
            return 200, {'only_fake_code': 'FAKE-CODE'}
        return 200, {'ok': True}


def registration_display(destination, result):
    return RegistrationDisplay(enrollment_code=result['only_fake_code'])


def fake_checker(seed, code, now, last):
    step = int(now // 30)
    return step if code == '123456' and step > last else None

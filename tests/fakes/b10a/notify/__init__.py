"""Socket-free notification fixtures; shared b10a fakes remain unchanged."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from axcommon.client import ApiError, LedgerClient
from axcommon.redact import Redactor
from core.hq.notify.actions import NotifyActions
from core.hq.notify.digest import MorningDigest
from core.hq.notify.dispatcher import Dispatcher
from core.hq.notify.guard import Guard
from core.hq.notify.intake import IntakeClient
from core.hq.notify.loop import NotifyHub
from core.hq.notify.ntfy import NtfyClient
from core.hq.notify.question import LedgerPort, QuestionNotifier
from core.hq.notify.store import NotifyStore
from core.ledger import LedgerService, Reject
from core.ledger.store import Store
from tests.fakes.b10a.world import World


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **delta):
        self.value += timedelta(**delta)


class Sources:
    def __init__(self):
        self.done = []
        self.promises = []
        self.missing = set()
        self.calls = []

    def done_on(self, date):
        self.calls.append(('done', date))
        if 'done' in self.missing:
            raise ApiError(503, 'unavailable')
        return self.done

    def open_promises(self):
        self.calls.append(('promises', None))
        if 'promises' in self.missing:
            raise ApiError(503, 'unavailable')
        return self.promises


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, kind, subject, data):
        self.events.append({'kind': kind, 'subject': subject, 'data': data})


class Harness:
    """Real notification code + real ledger rules behind in-memory transports."""
    def __init__(self, path=':memory:', *, policies=None):
        self.clock = Clock()
        self.world = World()
        self.store = NotifyStore(path)
        self.sink = Sink()
        self.redactor = Redactor(pii_list=['fixture-private-person'])
        self.sender = NtfyClient('http://ntfy.fixture:8908', transport=self.world)
        self.dispatcher = Dispatcher(self.store, {'ntfy': self.sender}, policies=policies,
                                     guard=Guard(self.redactor), sink=self.sink, now=self.clock,
                                     hq_base_url='https://hq.fixture.invalid')
        self.ledger_store = Store(':memory:')
        self.ledger_service = LedgerService(self.ledger_store, None, secret_scan=lambda value: [])
        self.world.extra['ledger'] = self.ledger_transport
        self.port = LedgerPort(LedgerClient('http://ledger:8080', transport=self.world))
        self.intake_calls = []
        self.apply = True
        self.rejection = None
        self.intake = IntakeClient(self.intake_transport)
        self.questions = QuestionNotifier(self.port, self.store, self.dispatcher, now=self.clock)
        self.sources = Sources()
        self.digest = MorningDigest(self.sources, self.store, self.dispatcher, now=self.clock, sink=self.sink)
        self.actions = NotifyActions(self.port, self.intake, self.store, now=self.clock)
        self.hub = NotifyHub(self.store, self.dispatcher, self.questions, self.digest, self.actions)

    def close(self):
        self.store.close()
        self.ledger_store.close()

    def todo(self, state='todo', **extra):
        return self.ledger_service.create_todo({'title': 'fixture task', 'state': state, **extra}, actor='fixture', via='hq')

    def question(self, **extra):
        return self.todo('waiting_user', card={'question': 'Choose A or B', 'options': ['A', 'B'],
                                              'waiting_reason': 'preference', **extra})

    def ledger_transport(self, method, path, query, body, headers):
        assert method == 'GET', 'Notification code must not write ledger directly'
        if path == '/v1/todos':
            return 200, {}, json.dumps({'todos': self.ledger_service.list_todos(state=query.get('state'))}).encode()
        todo = self.ledger_service.get_todo(path.rsplit('/', 1)[-1])
        if todo is None:
            return 404, {}, b'{"error":{"code":"not_found"}}'
        return 200, {}, json.dumps(todo).encode()

    def intake_transport(self, method, path, raw):
        assert (method, path) == ('POST', '/v1/instructions')
        body = json.loads(raw)
        assert body['from'] == 'hq'
        self.intake_calls.append(body)
        if self.rejection:
            status, code = self.rejection
            return status, json.dumps({'ok': False, 'error': {'code': code}}).encode()
        payload = body['payload']
        try:
            if self.apply and body['destination'] in ('ledger.complete', 'ledger.park'):
                transition = {'to': 'done' if body['destination'] == 'ledger.complete' else 'parked'}
                transition.update({key: payload[key] for key in ('reason', 'check_at') if key in payload})
                self.ledger_service.transition(payload['todo_id'], transition, actor='hq', via='hq')
            elif self.apply and body['destination'] == 'ledger.answer':
                answer = {key: payload[key] for key in ('answer', 'version') if key in payload}
                self.ledger_service.answer(payload['todo_id'], {**answer, 'answer_source': 'hq'}, actor='hq', via='hq')
        except Reject as exc:
            return exc.status, json.dumps({'ok': False, 'error': {'code': exc.code}}).encode()
        return 200, b'{"ok":true}'

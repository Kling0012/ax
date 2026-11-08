from types import SimpleNamespace

from starlette.applications import Starlette
from axcommon.events import build_event

from core.hq.approval.private import PrivateStore
from core.hq.approval.service import Identity
from core.hq.ext.b12b2 import BrowserPolicy, register
from core.hq.notify.dispatcher import Dispatcher
from core.hq.notify.ntfy import Receipt
from core.hq.notify.store import NotifyStore
from tests.fakes.b12b2.hq.parts import FakeIntake, Forms, call, fake_checker


class Sink:
    def __init__(self):
        self.events = {}

    def emit(self, kind, subject, data, *, event_id):
        event = build_event(component='hq', host='fake-host', kind=kind, subject=subject, data=data, event_id=event_id)
        self.events.setdefault(event_id, event)


class Sender:
    def __init__(self):
        self.ok = True
        self.messages = []

    def send(self, **message):
        if not self.ok:
            return Receipt(False, reason='offline', transient=True)
        self.messages.append(message)
        return Receipt(True, id='fake')


class Redactor:
    index_ok = pii_list_ok = True

    def redact_text(self, text):
        return SimpleNamespace(text=text, hits=[])


def test_public_factor_delivery_and_retry(tmp_path):
    from core.hq.notify.guard import Guard
    from core.hq.notify import DEST_NTFY

    private = tmp_path / 'hq'
    private.mkdir(mode=0o700)
    sender, sink = Sender(), Sink()
    store = NotifyStore(tmp_path / 'notify.sqlite')
    dispatcher = Dispatcher(store, {DEST_NTFY: sender}, guard=Guard(Redactor()))
    app = Starlette()
    app.state.sink = sink
    app.state.notify = SimpleNamespace(dispatcher=dispatcher)
    app.state.hq_factor_private_store = PrivateStore(private / 'private')
    app.state.hq_approval_intake = intake = FakeIntake()
    app.state.hq_approval_identity = lambda request: Identity('user', 'session', True)
    app.state.hq_approval_browser_policy = BrowserPolicy(frozenset({'hq.test'}), frozenset({'https://hq.test'}))
    register(app)
    app.state.hq_approval.factor.checker = fake_checker
    now = [1_800_000_000.0]
    app.state.hq_approval.factor.clock = lambda: now[0]

    def submit(path, page_path, code=None):
        page = call(app, 'GET', page_path)
        assert page['status'] == 200
        body = Forms(page['text']).values
        if code is not None:
            body['code'] = code
        return call(app, 'POST', path, body=body)

    assert submit('/v1/hq/approval/enroll', '/approval/settings')['status'] == 200
    assert len(sender.messages) == len(sink.events) == 1
    sender.ok = False
    assert submit('/v1/hq/approvals/approval-a/grant', '/approvals/approval-a', '000000')['status'] == 403
    assert len(sink.events) == 2
    assert submit('/v1/hq/approvals/approval-a/grant', '/approvals/approval-a', '123456')['status'] == 503
    assert not intake.executed and intake.saved['state'] == 'pending_user'
    assert len(sink.events) == 2 and len(sender.messages) == 1
    sender.ok = True
    row = store.by_dedupe('factor:' + app.state.hq_factor_private_store.read()['outbox'][0]['id'])
    store.update(row['id'], next_try_at=None)
    dispatcher.deliver_due()
    assert submit('/v1/hq/approvals/approval-a/grant', '/approvals/approval-a', '123456')['status'] == 200
    assert len(sender.messages) == len(sink.events) == 2
    assert all('123456' not in str(message) and '000000' not in str(message) and 'user' not in str(message)
               for message in sender.messages)
    now[0] += 30
    assert submit('/v1/hq/approval/enroll', '/approval/settings', '123456')['status'] == 200
    assert len(sender.messages) == len(sink.events) == 3
    disconnected = Starlette()
    disconnected.state.hq_factor_private_store = app.state.hq_factor_private_store
    disconnected.state.hq_approval_intake = intake
    disconnected.state.hq_approval_identity = app.state.hq_approval_identity
    disconnected.state.hq_approval_browser_policy = app.state.hq_approval_browser_policy
    register(disconnected)
    intake.saved['state'] = 'pending_user'
    page = call(disconnected, 'GET', '/approvals/approval-a')
    body = {**Forms(page['text']).values, 'code': '123456'}
    response = call(disconnected, 'POST', '/v1/hq/approvals/approval-a/grant', body=body)
    assert response['status'] == 503 and len(intake.executed) == 1

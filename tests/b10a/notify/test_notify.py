"""T-53/54/55/81 and digest/emergency boundaries, without network or services."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.applications import Starlette

from axcommon import schemas
from axcommon.client import Unavailable
from core.hq.ext import b10a
from core.hq.notify.digest import MorningDigest
from core.hq.notify.dispatcher import Notification
from core.hq.notify.guard import Guard
from core.hq.notify.ntfy import NtfyClient
from core.hq.notify.policy import DestinationPolicy
from core.ledger import Reject
from tests.fakes.b10a.asgi import call
from tests.fakes.b10a.notify import Harness


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path / 'notifications.sqlite')
    yield harness
    harness.close()


def note(**kw):
    return Notification(kind='todo', title='fixture notification', message='fixture body', **kw)


def app_for(h, auth=True):
    app = Starlette(routes=b10a.make_routes(h.hub, control=object()))
    if auth is not None:
        app.state.hq_auth = lambda request: {'id': 'fixture-user'} if auth else None
    return app


def test_t53_reason_required_and_answer_resumes_original_card(h):
    with pytest.raises(Reject) as rejected:
        h.todo('waiting_user', card={'question': 'Choose A', 'options': ['A']})
    assert rejected.value.code == 'waiting_reason_required'
    todo = h.question()
    assert h.questions.tick()['notified'] == 1
    h.clock.advance(hours=47, minutes=59, seconds=59)
    assert h.questions.tick()['suppressed'] == 1
    assert len(h.world.ntfy.sent) == 1
    h.clock.advance(seconds=1)
    assert h.questions.tick()['notified'] == 1
    assert len(h.world.ntfy.sent) == 2
    status, _ = h.intake.submit('ledger.answer', {'todo_id': todo['id'], 'answer': 'A', 'version': 1}, idempotency_key='answer-1')
    assert status == 200
    resumed = h.port.get(todo['id'])
    assert resumed['id'] == todo['id'] and resumed['state'] == 'todo'
    assert resumed['card']['answer']['answer'] == 'A'
    assert len(h.ledger_service.list_todos()) == 1
    h.clock.advance(hours=49)
    assert h.questions.tick()['notified'] == 0


def test_t53_cancel_answered_and_old_version_before_draining(h):
    todo = h.question()
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 0)
    h.questions.tick()
    held = h.store.list(todo_id=todo['id'])[0]
    h.intake.submit('ledger.answer', {'todo_id': todo['id'], 'answer': 'A'}, idempotency_key='answer')
    h.question()  # A new question must not flush the answered question first.
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 60)
    h.questions.tick()
    assert h.store.get(held['id'])['state'] == 'cancelled'
    assert len(h.world.ntfy.sent) == 1
    assert todo['id'] not in json.dumps(h.world.ntfy.sent)


def test_t54_open_allowed_actions_and_idempotent_complete(h):
    todo = h.todo('running')
    app = app_for(h)
    status, opened = call(app, 'GET', f"/v1/notify/open/{todo['id']}")
    assert status == 200 and opened['todo']['id'] == todo['id']
    assert set(opened['actions']) == {'complete', 'park'}
    request = {'todo_id': todo['id'], 'action': 'complete', 'op_id': 'complete-1'}
    status, result = call(app, 'POST', '/v1/notify/actions', body=request)
    assert status == 200 and result['state'] == h.port.get(todo['id'])['state'] == 'done'
    assert h.intake_calls[0]['destination'] == 'ledger.complete'
    status, replay = call(app, 'POST', '/v1/notify/actions', body=request)
    assert status == 200 and replay['replayed']
    assert len(h.intake_calls) == 1
    assert h.actions.apply(todo_id='other', action='complete', op_id='complete-1')['status'] == 409
    assert h.actions.apply(todo_id=todo['id'], action='park', op_id='bad')['error'] == 'invalid_transition'


def test_t54_park_validation_and_ledger_verification(h):
    todo = h.todo()
    assert h.actions.apply(todo_id=todo['id'], action='complete', op_id='no')['status'] == 409
    assert h.actions.apply(todo_id=todo['id'], action='park', op_id='missing')['status'] == 422
    assert h.actions.apply(todo_id=todo['id'], action='park', op_id='invalid', reason='fixture', check_at='bad')['status'] == 422
    args = dict(todo_id=todo['id'], action='park', op_id='park', reason='fixture', check_at='2026-11-08T00:00:00.000Z')
    h.apply = False
    assert h.actions.apply(**args)['error'] == 'ledger_not_updated'
    h.apply = True
    result = h.actions.apply(**args)
    assert result['ok'] and h.port.get(todo['id'])['state'] == 'parked'
    assert h.port.get(todo['id'])['check_at'] == args['check_at']


@pytest.mark.parametrize('changes', [{'op_id': None}, {'op_id': 7}, {'reason': {}}, {'check_at': []}, {'unexpected': True}])
def test_t54_malformed_action_payload_is_rejected(h, changes):
    body = {'todo_id': h.todo()['id'], 'action': 'park', 'op_id': 'bad', 'reason': 'fixture', 'check_at': '2026-11-08T00:00:00.000Z'}
    status, _ = call(app_for(h), 'POST', '/v1/notify/actions', body={**body, **changes})
    assert status == 422 and h.intake_calls == []


def test_t55_ntfy_private_post_200_and_id(h):
    todo = h.todo()
    result = h.dispatcher.submit(note(todo_id=todo['id']))
    row = h.store.get(result['id'])
    assert row['state'] == 'sent' and row['external_id'] == 'ntfy-1'
    assert row['occurred_at'] and row['todo_id'] == todo['id'] and row['kind'] == 'todo'
    sent = h.world.ntfy.sent[0]
    assert sent['topic'] == 'hub' and sent['click'].endswith(f"/#/todo/{todo['id']}")
    assert 'actions' not in sent
    assert set(h.world.calls) == {('ntfy.fixture', 'POST', '/')}
    h.world.ntfy.no_id = True
    assert h.sender.send(title='fixture', message='fixture').reason == 'no_id'


@pytest.mark.parametrize('status', [201, 202, 204])
def test_t55_only_http_200_is_acceptance(status):
    def transport(*args):
        return status, {}, b'{"id":"fixture-id"}'
    receipt = NtfyClient('http://ntfy.fixture', transport=transport).send_test()
    assert not receipt.ok and receipt.reason == f'http_{status}'


def test_t55_secret_pii_mask_and_discord_refusal(h):
    result = h.dispatcher.submit(Notification(kind='todo', title='fixture-private-person', message='fixture-private-person'))
    assert result['state'] == 'sent'
    assert 'fixture-private-person' not in json.dumps(h.world.ntfy.sent)
    h.dispatcher.senders['discord'] = h.sender
    result = h.dispatcher.submit(Notification(kind='todo', dest='discord', title='fixture', message='fixture-private-person'))
    assert result['state'] == 'blocked' and len(h.world.ntfy.sent) == 1
    failed = [e for e in h.sink.events if e['kind'] == 'notification.failed']
    assert failed and 'fixture-private-person' not in json.dumps(failed)


def test_t55_guard_failure_blocks_and_isolated_sender_failure(h):
    h.dispatcher.guard = Guard(None)
    assert h.dispatcher.submit(note())['state'] == 'blocked'
    assert h.world.ntfy.sent == []
    h.dispatcher.guard = Guard(h.redactor)
    h.redactor.pii_list_ok = False
    assert h.dispatcher.submit(note())['state'] == 'blocked'
    h.redactor.pii_list_ok = True
    class BrokenSender:
        def send(self, **kwargs):
            raise ValueError('fixture-private-person')
    h.dispatcher.senders['discord'] = BrokenSender()
    assert h.dispatcher.submit(note(dest='discord'))['state'] == 'pending'
    assert h.dispatcher.submit(note())['state'] == 'sent'
    assert 'fixture-private-person' not in json.dumps(h.sink.events)


def test_t55_metadata_is_inspected_and_discord_never_bypasses_guard(h):
    h.dispatcher.senders['discord'] = h.sender
    h.dispatcher.policies['discord'] = DestinationPolicy('discord', 60, on_hit='mask', on_unverifiable='send')
    h.dispatcher.guard = Guard(None)
    assert h.dispatcher.submit(note(dest='discord'))['state'] == 'blocked'
    h.dispatcher.guard = Guard(h.redactor)
    assert h.dispatcher.submit(note(tags=('fixture-private-person',)))['state'] == 'blocked'
    assert h.dispatcher.submit(note(ref='#/todo/fixture-private-person'))['state'] == 'blocked'
    assert h.world.ntfy.sent == []


def test_t81_per_destination_quota_and_persistent_undelivered(h):
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 4)
    h.dispatcher.policies['discord'] = DestinationPolicy('discord', 1, on_hit='block')
    h.dispatcher.senders['discord'] = h.sender
    results = [h.dispatcher.submit(note()) for _ in range(6)]
    assert [r['state'] for r in results] == ['sent'] * 4 + ['held'] * 2
    assert h.dispatcher.submit(note(dest='discord'))['state'] == 'sent'
    assert h.dispatcher.submit(note(dest='discord'))['state'] == 'held'
    assert len(h.store.list(state='held')) == 3
    h.clock.advance(seconds=61)
    assert h.dispatcher.deliver_due()['sent'] == 3
    assert len(h.world.ntfy.sent) == 8


def test_t81_retry_retains_notification_without_old_processes(h):
    h.world.ntfy.fail_next(1)
    result = h.dispatcher.submit(note())
    assert result['state'] == 'pending'
    assert h.store.get(result['id'])['attempts'] == 1
    assert h.dispatcher.deliver_due()['sent'] == 0
    h.clock.advance(seconds=31)
    assert h.dispatcher.deliver_due()['sent'] == 1
    assert len(h.store.list()) == 1 and len(h.world.ntfy.sent) == 1


def test_digest_jst_missing_data_and_once_per_day(h):
    h.clock.value = h.clock.value.replace(hour=21)  # Next day, 06:00 JST.
    assert h.digest.run_if_due()['state'] == 'not_due'
    h.clock.advance(hours=1)
    h.sources.missing.add('done')
    h.sources.promises = [{'text': 'fixture promise', 'todo_id': 'fixture', 'due_at': '2026-11-08T00:00:00.000Z'}]
    result = h.digest.run_if_due()
    assert result['date'] == '2026-11-07' and result['missing'] == ['done']
    assert '取得できなかった' in h.world.ntfy.sent[0]['message']
    assert 'fixture promise' in h.world.ntfy.sent[0]['message']
    assert h.sink.events[-1]['data']['counts'] == {'open_promises': 1}
    assert h.digest.run_if_due()['state'] == 'sent'
    reloaded = MorningDigest(h.sources, h.store, h.dispatcher, now=h.clock)
    assert reloaded.run_if_due()['state'] == 'sent'
    assert len(h.world.ntfy.sent) == 1


def test_digest_failure_reconciles_delivery_and_handles_index_crash(h):
    h.world.ntfy.fail_next(1)
    first = h.digest.run_if_due()
    assert first['state'] == 'pending'
    assert h.digest.run_if_due()['state'] == 'pending'
    h.clock.advance(seconds=31)
    h.dispatcher.deliver_due()
    assert h.digest.run_if_due()['state'] == 'sent'
    assert len(h.world.ntfy.sent) == 1
    # Simulate a persisted sent notification whose digest index was not committed.
    h.clock.advance(days=1)
    date = h.digest.target_date(h.clock())
    h.dispatcher.submit(Notification(kind='digest', title='fixture', message='fixture', dedupe_key=f'digest:{date}'))
    h.digest.run_if_due()
    assert len(h.world.ntfy.sent) == 2


def test_hq_auth_missing_denied_and_emergency_requires_confirmation(h):
    for auth, expected in [(None, 503), (False, 403)]:
        app = app_for(h, auth)
        assert call(app, 'GET', '/v1/notify/status')[0] == expected
        assert call(app, 'POST', '/v1/emergency', body={'scope': 'all', 'confirm': True, 'op_id': 'x'})[0] == expected
    app = app_for(h)
    assert call(app, 'POST', '/v1/emergency', body={'scope': 'all', 'op_id': 'x'})[0] == 422
    assert call(app, 'POST', '/v1/emergency', body={'scope': '../wrong', 'confirm': True, 'op_id': 'x'})[0] == 422
    body = {'scope': 'commanders', 'confirm': True, 'op_id': 'emergency-1'}
    assert call(app, 'POST', '/v1/emergency', body=body)[0] == 200
    instruction = h.intake_calls[-1]
    assert instruction['from'] == 'hq' and instruction['destination'] == 'emergency'
    assert instruction['payload'] == {'scope': 'commanders'} and instruction['confirm_irreversible'] is True


def test_hq_test_notification_is_once_and_authenticated(h):
    app = app_for(h)
    assert call(app, 'POST', '/v1/notify/test')[0] == 200
    assert call(app, 'POST', '/v1/notify/test')[0] == 409
    assert len(h.world.ntfy.sent) == 1


def test_hq_auth_exception_rejects_and_async_auth_works(h):
    app = app_for(h)
    def broken(request):
        raise Unavailable('fixture')
    app.state.hq_auth = broken
    assert call(app, 'GET', '/v1/notify/status')[0] == 503
    async def authorized(request):
        return {'id': 'fixture-user'}
    app.state.hq_auth = authorized
    assert call(app, 'GET', '/v1/notify/status')[0] == 200


def test_register_extension_without_starting_background_service(h, monkeypatch):
    monkeypatch.setenv('AX_HQ_NOTIFY_RUN', '0')
    monkeypatch.setattr(b10a, 'build_hub', lambda **kwargs: h.hub)
    monkeypatch.setattr(b10a, '_redactor_from_env', lambda: h.redactor)
    app = Starlette()
    app.state.hq_auth = lambda request: {'id': 'fixture-user'}
    b10a.register(app)
    assert app.state.notify is h.hub
    assert call(app, 'GET', '/v1/notify/status')[0] == 200
    assert not hasattr(app.state, 'notify_runner')


def test_schema_check_at_resolves_without_network():
    body = {'todo_id': 'fixture', 'action': 'park', 'op_id': 'fixture', 'check_at': '2026-11-08T00:00:00.000Z'}
    assert schemas.errors('ext/b10a/notify-action', body) == []


def test_t55_scan_before_shortening_and_never_persist_secret(h):
    secret = 'fixture-private-person'
    body = 'x' * 195 + ' ' + secret + ' suffix'
    result = h.dispatcher.submit(Notification(kind='todo', title='fixture', message=body))
    row = h.store.get(result['id'])
    assert secret not in row['message']
    assert secret[:3] not in h.world.ntfy.sent[-1]['message']
    h.dispatcher.senders['discord'] = h.sender
    result = h.dispatcher.submit(Notification(kind='todo', dest='discord', title='fixture', message=body))
    assert result['state'] == 'blocked'
    assert h.store.get(result['id'])['message'] == ''


def test_t81_quota_and_dedupe_are_serialized_across_threads(h):
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: h.dispatcher.submit(note()), range(12)))
    assert sum(r['state'] == 'sent' for r in results) == 1
    assert len(h.store.list(state='held')) == 11
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: h.dispatcher.submit(note(dedupe_key='same')), range(12)))
    assert sum(r['state'] == 'duplicate' for r in results) == 11


def test_digest_invalid_success_response_is_missing_not_empty(h):
    from core.hq.notify.digest import LedgerDigestSources
    from axcommon.client import LedgerClient
    def malformed(*args):
        return 200, {}, b'{}'
    sources = LedgerDigestSources(LedgerClient('http://ledger.fixture', transport=malformed))
    digest = MorningDigest(sources, h.store, h.dispatcher, now=h.clock)
    _, body, missing, counts = digest.build('2026-11-06')
    assert missing == ['done', 'open_promises'] and counts == {}
    assert '0件' not in body


def test_digest_persisted_outbox_survives_new_store_instance(h, tmp_path):
    from core.hq.notify.store import NotifyStore
    from core.hq.notify.dispatcher import Dispatcher
    h.world.ntfy.fail_next(1)
    original = h.digest.run_if_due()
    reopened = NotifyStore(tmp_path / 'notifications.sqlite')
    try:
        dispatcher = Dispatcher(reopened, {'ntfy': h.sender}, guard=Guard(h.redactor), now=h.clock)
        digest = MorningDigest(h.sources, reopened, dispatcher, now=h.clock)
        assert digest.run_if_due()['state'] == 'pending'
        h.clock.advance(seconds=31)
        assert dispatcher.deliver_due()['sent'] == 1
        assert digest.run_if_due()['state'] == 'sent'
        assert len(h.world.ntfy.sent) == 1 and reopened.get(original['notification_id'])['state'] == 'sent'
    finally:
        reopened.close()


def test_digest_counts_and_missing_notice_precede_long_titles(h):
    h.sources.done = [{'title': 'x' * 400}]
    h.sources.missing.add('promises')
    h.digest.run_if_due()
    body = h.world.ntfy.sent[0]['message']
    assert '完了: 1件' in body and '未完了の約束: 取得できなかった' in body


def test_hq_emergency_propagates_intake_rejection(h):
    h.rejection = (409, 'snapshot_failed')
    status, body = call(app_for(h), 'POST', '/v1/emergency', body={'scope': 'all', 'confirm': True, 'op_id': 'x'})
    assert status == 409 and body['error']['code'] == 'snapshot_failed'


def test_hq_emergency_scope_lookup_failure_keeps_fixed_scopes(h):
    from axcommon.client import ApiClient
    def unavailable(*args):
        raise Unavailable('fixture')
    control = ApiClient('http://control.fixture', transport=unavailable)
    app = Starlette(routes=b10a.make_routes(h.hub, control=control))
    app.state.hq_auth = lambda request: {'id': 'fixture-user'}
    status, body = call(app, 'GET', '/v1/emergency/scopes')
    assert status == 200 and body == {'scopes': ['all', 'commanders'], 'plugins_unavailable': True}


def test_register_defers_runner_until_hq_lifecycle(h, monkeypatch):
    events = []
    class FakeRunner:
        def __init__(self, hub):
            assert hub is h.hub
        def start(self):
            events.append('start')
        def stop(self):
            events.append('stop')
    monkeypatch.setenv('AX_HQ_NOTIFY_RUN', '1')
    monkeypatch.setattr(b10a, 'build_hub', lambda **kwargs: h.hub)
    monkeypatch.setattr(b10a, '_redactor_from_env', lambda: h.redactor)
    monkeypatch.setattr(b10a, 'Runner', FakeRunner)
    app = Starlette()
    b10a.register(app)
    assert events == []
    import asyncio
    async def lifecycle():
        async with app.router.lifespan_context(app):
            assert events == ['start']
    asyncio.run(lifecycle())
    assert events == ['start', 'stop']


def test_knobs_apply_and_invalid_values_do_not_replace_valid_settings(h):
    assert h.hub.apply_knobs({'question_renotify_h': 72, 'morning_digest_time_jst': '08:30 JST'}) == [
        'question_renotify_h', 'morning_digest_time_jst']
    assert h.questions.renotify_h == 72 and h.digest.time_text == '08:30 JST'
    assert h.hub.apply_knobs({'question_renotify_h': -1, 'morning_digest_time_jst': '25:00'}) == []
    assert h.questions.renotify_h == 72 and h.digest.time_text == '08:30 JST'


def test_t55_changed_pii_list_is_reloaded_before_delivery(h, tmp_path):
    from axcommon.redact import Redactor
    path = tmp_path / 'pii-list'
    path.write_text('fixture-one\n')
    h.dispatcher.guard = Guard(Redactor(pii_list_path=path))
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 0)
    result = h.dispatcher.submit(Notification(kind='todo', title='fixture', message='fixture-two'))
    assert result['state'] == 'held'
    path.write_text('fixture-one\nfixture-two\n')
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 60)
    h.dispatcher.deliver_due()
    assert 'fixture-two' not in json.dumps(h.world.ntfy.sent)


def test_t55_missing_redaction_configuration_is_unverifiable(monkeypatch):
    monkeypatch.delenv('AX_REDACT_INDEX', raising=False)
    monkeypatch.delenv('AX_PII_LIST', raising=False)
    assert b10a._redactor_from_env() is None


def test_t55_concurrent_test_notifications_do_not_duplicate(h):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: h.dispatcher.send_test_once(), range(8)))
    assert sum(r['ok'] for r in results) == 1 and len(h.world.ntfy.sent) == 1


def test_t53_invalid_response_does_not_cancel_pending_questions(h, monkeypatch):
    h.question()
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 0)
    h.questions.tick()
    monkeypatch.setattr(h.port.client, 'list_todos', lambda **kwargs: {})
    assert h.questions.tick()['error'] == 'Unavailable'
    assert h.store.counts() == {'held': 1}


def test_digest_blocked_state_is_reported_without_resubmission(h):
    h.dispatcher.guard = Guard(None)
    assert h.digest.run_if_due()['state'] == 'blocked'
    assert h.digest.run_if_due()['state'] == 'blocked'
    assert h.store.counts() == {'blocked': 1}
    assert h.world.ntfy.sent == []


def test_t53_new_question_version_cancels_held_old_version(h, monkeypatch):
    todo = h.question()
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 0)
    h.questions.tick()
    old = h.store.list(todo_id=todo['id'])[0]
    changed = {**todo, 'card': {**todo['card'], 'version': 2, 'question': 'Choose C or D'}}
    monkeypatch.setattr(h.port, 'waiting', lambda: [changed])
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 60)
    assert h.questions.tick()['notified'] == 1
    assert h.store.get(old['id'])['state'] == 'cancelled'
    assert h.world.ntfy.sent[0]['message'] == 'Choose C or D'
    assert len(h.world.ntfy.sent) == 1

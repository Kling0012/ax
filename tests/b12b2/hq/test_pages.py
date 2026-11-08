from __future__ import annotations

from copy import deepcopy
from html import escape
import json

import pytest

from core.hq.approval.intake import UnixIntake
from core.hq.approval.service import Identity, VerifiedAction
from core.hq.seats.service import SeatService
from tests.fakes.b12b2.hq.parts import Forms, call


def screen(world, path='/approvals/approval-a'):
    reply = call(world['app'], 'GET', path)
    assert reply['status'] == 200, reply['text']
    return Forms(reply['text']).values


def post(world, path, *, page=None, **extra):
    page = screen(world) if page is None else page
    return call(world['app'], 'POST', path, body={**page, **extra})


def test_t189_saved_grant_executes_once(world):
    page = screen(world)
    reply = post(world, '/v1/hq/approvals/approval-a/grant', page=page, code='123456')
    assert reply['status'] == 200
    command = world['intake'].executed[0]
    assert command['issued_by'] == 'user'
    assert command['approved_via'] == 'approval-a'
    assert command['payload']['target'] == 'module'
    assert post(world, '/v1/hq/approvals/approval-a/grant', page=page, code='123456')['status'] == 403
    assert len(world['intake'].executed) == 1


def test_t189_deny_and_expiry_do_not_execute(world):
    reply = post(world, '/v1/hq/approvals/approval-a/deny', code='123456')
    assert reply['status'] == 200 and world['intake'].denied
    assert not world['intake'].executed
    world['now'][0] += 30
    world['intake'].saved['state'] = 'pending_user'
    world['intake'].expired = True
    assert post(world, '/v1/hq/approvals/approval-a/grant', code='123456')['status'] == 409
    assert not world['intake'].executed


def test_t189_digest_is_bound_to_display(world):
    page = screen(world)
    world['intake'].saved['digest'] = 'b' * 64
    assert post(world, '/v1/hq/approvals/approval-a/grant', page=page, code='123456')['status'] == 409
    assert not world['intake'].executed


@pytest.mark.parametrize('extra', [{'digest': 'b' * 64}, {'payload': {'target': 'other'}},
                                  {'confirm_irreversible': True}, {'second_factor': True}])
def test_t190_reject_client_authority(world, extra):
    reply = post(world, '/v1/hq/approvals/approval-a/grant', code='123456', **extra)
    assert reply['status'] == 422
    assert not world['intake'].executed


@pytest.mark.parametrize('identity', [None, {}, Identity('opaque-user', 'session-a', True, False)])
def test_t190_requires_trusted_allowed_identity(world, identity):
    world['app'].state.hq_approval_identity = lambda request: identity
    assert call(world['app'], 'GET', '/approvals/approval-a')['status'] == 403


@pytest.mark.parametrize('path,method', [('/approvals/approval-a', 'GET'), ('/seats', 'GET'),
    ('/approval/settings', 'GET'), ('/v1/hq/approval/enroll', 'POST'),
    ('/v1/hq/approvals/approval-a/grant', 'POST'), ('/v1/hq/approvals/approval-a/deny', 'POST'),
    ('/v1/hq/seats/prepare', 'POST'), ('/v1/hq/seats/apply', 'POST')])
def test_t190_public_route_is_404(world, path, method):
    world['app'].state.hq_approval_identity = lambda request: Identity('public-user', 's', False)
    assert call(world['app'], method, path, body={})['status'] == 404
    assert not world['intake'].executed and not world['intake'].seat_calls


@pytest.mark.parametrize('headers,status', [({'host': 'evil.test'}, 421),
    ({'origin': None}, 403), ({'origin': 'https://evil.test'}, 403),
    ({'sec-fetch-site': 'cross-site'}, 403), ({'sec-fetch-site': 'same-site'}, 403)])
def test_t190_browser_request_checks(world, headers, status):
    page = screen(world)
    reply = call(world['app'], 'POST', '/v1/hq/approvals/approval-a/grant',
                 body={**page, 'code': '123456'}, headers=headers)
    assert reply['status'] == status
    assert not world['intake'].executed


def test_t190_csrf_bound_to_session_screen_and_time(world):
    page = screen(world)
    assert post(world, '/v1/hq/approval/enroll', page=page, code='123456')['status'] == 403
    world['app'].state.hq_approval_identity = lambda request: Identity('opaque-user', 'session-b', True)
    assert post(world, '/v1/hq/approvals/approval-a/grant', page=page, code='123456')['status'] == 403
    world['app'].state.hq_approval_identity = lambda request: world['identity']
    world['now'][0] += 901
    assert post(world, '/v1/hq/approvals/approval-a/grant', page=page, code='123456')['status'] == 403


def test_t190_requires_factor_and_locks_at_five(world):
    assert post(world, '/v1/hq/approvals/approval-a/grant')['status'] == 403
    for _ in range(4):
        assert post(world, '/v1/hq/approvals/approval-a/grant', code='000000')['status'] == 403
    assert world['factor'].status()['locked_until'] == world['now'][0] + 900
    assert post(world, '/v1/hq/approvals/approval-a/grant', code='123456')['status'] == 403
    assert len([e for e in world['events'] if e['kind'] == 'auth.failed']) == 2
    assert world['events'][-1]['data']['reason'] == 'approval_locked'
    assert len(world['events']) == len(world['notifications'])
    world['now'][0] += 901
    assert post(world, '/v1/hq/approvals/approval-a/grant', code='123456')['status'] == 200


def test_t193_reason_is_separate_and_escaped(world):
    text = call(world['app'], 'GET', '/approvals/approval-a')['text']
    assert '<script>unsafe' not in text
    assert '&lt;script&gt;unsafe' in text
    assert 'id="fixed-fields"' in text and 'id="untrusted-reason"' in text
    assert '司令塔の説明（未検証）' in text
    assert text.index('id="untrusted-reason"') > text.index('id="fixed-fields"')


def test_t194_saved_go_full_text_and_derived_values(world):
    text = '# GO\n原文: </pre><script>untrusted()</script>\n範囲: 構築\n' + '全文\n' * 100
    world['intake'].saved.update(destination='go.confirm', payload={'kind': 'ratify', 'go_md_text': text,
           'go_md_sha256': 'c' * 64, 'reason': '未検証'}, derived={'scope': 'build', 'excluded': [], 'changes': [1, 2]})
    reply = call(world['app'], 'GET', '/approvals/approval-a?scope=both&go_md_text=forged')
    assert reply['status'] == 200
    assert escape(text, quote=True) in reply['text']
    assert '<script>untrusted' not in reply['text'] and '<details' not in reply['text']
    assert 'cccccccccccc' in reply['text'] and 'build' in reply['text']
    assert 'forged' not in reply['text']
    assert 'no-store' in reply['headers'][b'cache-control'].decode()


def seat_prepare(world, dest, payload):
    page = screen(world, '/seats')
    reply = post(world, '/v1/hq/seats/prepare', page=page, destination=dest, payload=payload)
    assert reply['status'] == 200, reply['text']
    return Forms(reply['text']).values


@pytest.mark.parametrize('dest,payload', [('seat.revoke', {'seat_id': 'seat-a'}), ('seat.lock', {'scope': 'all'})])
def test_s2210_revocation_and_lock_need_no_factor(world, dest, payload):
    page = seat_prepare(world, dest, payload)
    assert post(world, '/v1/hq/seats/apply', page=page)['status'] == 200
    assert world['intake'].seat_calls[-1][0:2] == (dest, payload)
    assert world['intake'].seat_calls[-1][2].second_factor is False


@pytest.mark.parametrize('dest,payload', [('seat.tier', {'seat_id': 'seat-a', 'tier': 'operate'}),
    ('seat.unlock', {'scope': 'tailnet'}), ('seat.enroll_start', {'label': 'device', 'tier': 'read'})])
def test_s2210_registration_tier_unlock_require_factor(world, dest, payload):
    page = seat_prepare(world, dest, payload)
    assert post(world, '/v1/hq/seats/apply', page=page)['status'] == 403
    assert not world['intake'].seat_calls
    page = seat_prepare(world, dest, payload)
    assert post(world, '/v1/hq/seats/apply', page=page, code='123456')['status'] == 200
    assert world['intake'].seat_calls[-1][2].second_factor is True


def test_s2210_registration_code_display_once(world):
    page = seat_prepare(world, 'seat.enroll_start', {'label': 'device', 'tier': 'read'})
    first = post(world, '/v1/hq/seats/apply', page=page, code='123456')
    assert first['status'] == 200 and 'FAKE-CODE' in first['text']
    assert post(world, '/v1/hq/seats/apply', page=page, code='123456')['status'] == 403
    assert 'FAKE-CODE' not in call(world['app'], 'GET', '/seats')['text']
    assert 'FAKE-CODE' not in json.dumps(world['events'] + world['notifications'])


def test_s2210_client_cannot_replace_prepared_payload(world):
    page = seat_prepare(world, 'seat.lock', {'scope': 'ssh'})
    assert post(world, '/v1/hq/seats/apply', page=page, payload={'scope': 'all'})['status'] == 422
    assert not world['intake'].seat_calls


def test_unresolved_bindings_fail_closed(world):
    del world['app'].state.hq_approval_identity
    assert call(world['app'], 'GET', '/seats')['status'] == 501
    with pytest.raises(NotImplementedError):
        UnixIntake(transport=lambda *args: pytest.fail('wire must not be used')).grant('approval-a', 'digest', VerifiedAction(world['identity'], True))
    world['app'].state.hq_approval.seats.registration_display = None
    with pytest.raises(NotImplementedError, match='registration response binding missing'):
        world['app'].state.hq_approval.seats.prepare('seat.add', {
            'label': 'test-seat',
            'device': 'test-device',
            'device_id': 'test-device-id',
            'public_key': 'ssh-ed25519 ZmFrZQ==',
            'tier': 'read',
        })


def test_s2210_existing_seat_fields_and_number_overlay_source(world):
    reply = call(world['app'], 'GET', '/seats')
    assert reply['status'] == 200
    assert '&lt;script&gt;seat' in reply['text']
    for text in ('ラベル', '受け口', '端末', '期限', '最終使用', '公開鍵の指紋', 'auth.failed'):
        assert text in reply['text']
    script = call(world['app'], 'GET', '/v1/hq/b12b2/ui.js')['text']
    assert 'KeyN' in script and 'textContent' in script
    assert 'innerHTML' not in script


def test_seat_pending_request_must_equal_display_before_grant(world):
    service = SeatService(world['intake'])
    old = world['intake'].submit
    world['intake'].submit = lambda *a, **k: (202, {'approval_id': 'approval-a'})
    with pytest.raises(Exception) as error:
        service.apply({'destination': 'seat.tier', 'payload': {'tier': 'read'}, 'request_id': 'x'},
                      VerifiedAction(world['identity'], True))
    assert getattr(error.value, 'code', None) == 'approval_mismatch'
    assert not world['intake'].executed

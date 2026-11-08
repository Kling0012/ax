"""Provider acceptance is bound to the exact persisted delivery, not device delivery."""
from plugins.subloop.change.integration.gate import MergeGate
from tests.b14.integration.test_factory_hq_receipt_e2e import connected
from tests.b14.integration.test_standard_factory_e2e import server
from tests.fakes.b14.integration import HEAD, NEXT, Permission, Safety, Verification, item, snapshot, target


def gate_inputs():
    request = item(trusted=False)
    live = snapshot(request)
    safety = Safety().review({**request, 'snapshot': live})
    verification = Verification().verify(request, target(), head_sha=HEAD, base_sha=live['base_sha'])
    permissions = Permission(); permissions.authorize(request)
    return request, live, safety, verification, permissions.grant


def test_hq_acceptance_waits_30_minutes_without_claiming_device_delivery(connected):
    c = connected
    body = {'repo': 'owner/code', 'pr': 1, 'head_sha': HEAD, 'instruction_ref': 'instruction-1'}
    receipt = c.flow.notices.deliver('notice-accepted', 'user', body)
    assert receipt['status'] == 'unknown' and receipt['accepted'] is True
    assert 'delivered_at' not in receipt and len(c.hq.world.ntfy.sent) == 1
    now = receipt['accepted_at']
    notice = {**receipt, 'head_sha': HEAD, 'instruction_ref': 'instruction-1'}
    request, live, safety, verification, instruction = gate_inputs()
    gate = MergeGate()
    def decision(when, **updates):
        return gate.evaluate(request, target(), live, safety, verification, {'allowed': True},
                             now=when, instruction=instruction, notification={**notice, **updates})
    assert decision(now + 1799).reason == 'merge_notice_wait'
    assert decision(now + 1800).allowed
    assert decision(now + 1800, head_sha=NEXT).reason == 'merge_notice_stale'
    assert decision(now + 1800, instruction_ref='other').reason == 'merge_notice_stale'
    assert c.flow.notices.deliver('notice-accepted', 'user', body) == receipt
    assert c.flow.notices.deliver('notice-accepted', 'user', {**body, 'pr': 2})['reason'] == 'delivery_identity_mismatch'
    assert len(c.hq.world.ntfy.sent) == 1


def test_lost_response_and_conflicting_existing_payload_never_start_wait(connected, monkeypatch):
    c = connected
    api = c.flow.ports.notifications.backend.api
    body = {'repo': 'owner/code', 'pr': 1, 'head_sha': HEAD, 'instruction_ref': 'instruction-1'}
    api.post('/v1/notify/deliveries', {'delivery_id': 'collision', 'destination': 'user',
                                      'body': {**body, 'pr': 2}, 'mode': 'run'})
    conflict = c.flow.notices.deliver('collision', 'user', body)
    assert conflict.get('accepted') is not True
    original = api.post
    def lost(path, payload):
        value = original(path, payload)
        if path == '/v1/notify/deliveries':
            raise OSError('lost response')
        return value
    monkeypatch.setattr(api, 'post', lost)
    result = c.flow.notices.deliver('lost-notice', 'user', body)
    assert result.get('accepted') is not True
    assert c.flow.ports.notifications.reconcile('lost-notice', 'user').get('accepted') is not True

"""HQ routes against the real saved-approval service, with offline adapters."""
from __future__ import annotations

import pytest

from core.hq.approval.service import ApprovalFailure
from core.intake.approval import ApprovalService, ApprovalStore, VerifiedUser
from core.intake.approval.policy import AuthorityPolicy
from core.intake.approval.store import ApprovalError
from tests.fakes.b12b2.hq.parts import Forms, call
from tests.fakes.b12b2.intake.parts import (HQ, FakeExecutor, FakeSeatState, destination,
                                           instruction, seat, schema_for, ratify, go_text)


@pytest.fixture
def real(world, fake_clock):
    store = ApprovalStore(':memory:')
    executor, seats = FakeExecutor(), FakeSeatState()
    service = ApprovalService(store, policy=AuthorityPolicy(schema_loader=schema_for, seat_check=seats), executor=executor)
    class Port:
        @staticmethod
        def approval(aid, proof):
            return service.view(aid)
        @staticmethod
        def grant(aid, digest, proof):
            try:
                return 200, service.grant(aid, digest, HQ, VerifiedUser(proof.identity.allowed, proof.second_factor, proof.identity.direct_tailnet))
            except ApprovalError as exc:
                raise ApprovalFailure(exc.status, exc.reason) from None
        @staticmethod
        def deny(aid, digest, proof, **kw):
            return 200, service.deny(aid, HQ, VerifiedUser(True, proof.second_factor, True))
    world['app'].state.hq_approval.intake = Port()
    yield world, service, executor, fake_clock, seats
    store.close()


def grant(world, aid):
    page = call(world['app'], 'GET', '/approvals/' + aid)
    assert page['status'] == 200, page['text']
    return call(world['app'], 'POST', '/v1/hq/approvals/' + aid + '/grant',
                body={**Forms(page['text']).values, 'code': '123456'})


def test_t189_real_intake_saved_execution(real):
    world, service, executor, clock, seats = real
    aid = service.submit(instruction(payload={'target': 'original', 'reason': 'untrusted explanation'}), destination(), seat())['approval_id']
    assert grant(world, aid)['status'] == 200
    assert executor.calls[0]['payload']['target'] == 'original'
    assert executor.calls[0]['issued_by'] == 'user' and executor.calls[0]['approved_via'] == aid
    assert service.view(aid)['data']['state'] == 'executed'
    world['now'][0] += 30
    assert grant(world, aid)['status'] in {403, 409}
    assert len(executor.calls) == 1


def test_t189_real_intake_expired_never_executes(real):
    world, service, executor, clock, seats = real
    aid = service.submit(instruction(), destination(), seat())['approval_id']
    clock.advance(3601)
    assert grant(world, aid)['status'] in {403, 409}
    assert not executor.calls
    assert service.view(aid)['data']['state'] == 'expired'


def test_t194_real_intake_go_fulltext_derived_view(real):
    world, service, executor, clock, seats = real
    text = go_text(quote='</pre><script>do_not_execute()</script>')
    aid = service.submit(instruction('go.confirm', ratify(text)), destination('go.confirm'), seat())['approval_id']
    page = call(world['app'], 'GET', '/approvals/' + aid)
    assert page['status'] == 200
    assert '&lt;/pre&gt;&lt;script&gt;' in page['text'] and 'build' in page['text']
    assert grant(world, aid)['status'] == 200
    assert service.view(aid)['data']['go']['scope'] == 'build'
    assert not executor.calls

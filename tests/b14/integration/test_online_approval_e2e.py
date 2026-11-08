"""Real HQ/intake approvals wired into factory-created change ports."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.b12b2.intake.test_change_approval_e2e import setup_apps
from tests.b12b2.intake.test_hq_proof_e2e import system
from tests.b14.integration.test_standard_factory_e2e import runtime, server
from tests.b14.integration.test_t27_merge import receipts
from tests.fakes.b12b2.hq.parts import Forms
from tests.fakes.b14.integration import HEAD, NEXT, item, target


@pytest.fixture
def wired(system, tmp_path, monkeypatch, runtime, harness):
    client, component, browser = setup_apps(system, tmp_path, monkeypatch)
    ex, flow, state = runtime
    permission = flow.ports.permissions
    permission.client = client
    permission.control = SimpleNamespace(check_permission=lambda body: {'allowed': True})
    harness.ports.permissions = permission
    return harness, permission, browser, component, system


def saved(permission, operation):
    with permission.journal.lock:
        rows = permission.journal.db.execute("SELECT body FROM state WHERE kind='online_approval'").fetchall()
    return next(json.loads(row[0]) for row in rows if json.loads(row[0])['scope']['operation'] == operation)


def respond(browser, approval_id, decision='grant'):
    page = browser.get('/approvals/' + approval_id)
    response = browser.post('/v1/hq/approvals/' + approval_id + '/' + decision,
                            headers={'origin': 'https://hq.test'},
                            json={**Forms(page.text).values, 'code': '123456'})
    assert response.status_code == 200, response.text


def test_person_grant_allows_merge_component_cannot_grant(wired):
    h, permission, browser, component, system = wired
    _, safe, verified = receipts(h)
    assert h.flow.merge(item(), target(), safe, verified)['reason'] == 'individual_approval_required'
    record = saved(permission, 'merge')
    assert not h.github.merges
    assert component.post('/v1/hq/approvals/' + record['approval_id'] + '/grant', json={},
                          headers={'Authorization': 'Bearer fixture-change-token'}).status_code == 404
    respond(browser, record['approval_id'])
    assert h.flow.merge(item(), target(), safe, verified)['reason'] == 'release_pending'
    assert h.github.merges == [('owner/code', 1, HEAD, 'merge')]
    assert not system['executed']


def test_denial_disconnect_and_new_head_cannot_merge(wired, monkeypatch):
    h, permission, browser, component, system = wired
    _, safe, verified = receipts(h)
    h.flow.merge(item(), target(), safe, verified)
    record = saved(permission, 'merge')
    respond(browser, record['approval_id'], 'deny')
    assert h.flow.merge(item(), target(), safe, verified)['reason'] == 'individual_approval_required'
    monkeypatch.setattr(permission.client, 'check', lambda *a: {'status': 'unknown'})
    assert h.flow.merge(item(), target(), safe, verified)['reason'] == 'individual_approval_required'
    h.github.live['head_sha'] = NEXT
    assert h.flow.merge(item(), target(), safe, verified)['reason'] == 'individual_approval_required'
    assert not h.github.merges and not system['executed']


def test_expired_grant_not_used(wired):
    h, permission, browser, component, system = wired
    _, safe, verified = receipts(h)
    h.flow.merge(item(), target(), safe, verified)
    record = saved(permission, 'merge')
    respond(browser, record['approval_id'])
    permission.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=2)
    assert h.flow.merge(item(), target(), safe, verified)['reason'] == 'individual_approval_required'
    assert not h.github.merges and not system['executed']


def test_pr_update_requires_matching_person_grant(wired):
    h, permission, browser, component, system = wired
    live, safe, _ = receipts(h)
    assert h.flow.publish(item(), target(), safe, live)['reason'] == 'individual_approval_required'
    record = saved(permission, 'pr_update')
    assert record['scope']['input_revision'] == live['input_revision'] and not h.publications.sent
    respond(browser, record['approval_id'])
    assert h.flow.publish(item(), target(), safe, live)['status'] == 'delivered'
    assert len(h.publications.sent) == 1 and not system['executed']


def test_comment_requires_matching_person_grant(wired):
    h, permission, browser, component, system = wired
    it = item(route='question')
    assert h.flow._question(it, target())['reason'] == 'individual_approval_required'
    record = saved(permission, 'comment')
    assert record['scope']['repo'] == it['issue_repo'] and not h.publications.sent
    respond(browser, record['approval_id'])
    assert h.flow._question(it, target())['reason'] == 'question_answered'
    assert len(h.publications.sent) == 1 and not system['executed']


def test_lost_request_reuses_exact_id_and_scope(wired, monkeypatch):
    h, permission, browser, component, system = wired
    original = permission.client.request
    calls = []
    def lost_once(request_id, scope):
        calls.append((request_id, scope.copy()))
        response = original(request_id, scope)
        if len(calls) == 1:
            raise OSError('response lost')
        return response
    monkeypatch.setattr(permission.client, 'request', lost_once)
    _, safe, verified = receipts(h)
    assert h.flow.merge(item(), target(), safe, verified)['reason'] == 'individual_approval_required'
    assert h.flow.merge(item(), target(), safe, verified)['reason'] == 'individual_approval_required'
    assert len(calls) == 2 and calls[0] == calls[1]
    assert saved(permission, 'merge')['approval_id'] and not h.github.merges


def test_approved_old_head_never_authorizes_new_head(wired):
    h, permission, browser, component, system = wired
    _, safe, verified = receipts(h)
    h.flow.merge(item(), target(), safe, verified)
    respond(browser, saved(permission, 'merge')['approval_id'])
    h.github.live['head_sha'] = NEXT
    assert h.flow.merge(item(), target(), safe, verified)['reason'] == 'individual_approval_required'
    assert not h.github.merges and not system['executed']


def test_merge_checks_approval_again_immediately_before_write(wired, monkeypatch):
    h, permission, browser, component, system = wired
    _, safe, verified = receipts(h)
    h.flow.merge(item(), target(), safe, verified)
    respond(browser, saved(permission, 'merge')['approval_id'])
    original = permission.client.check
    checks = []
    def revoke_at_last(approval_id, scope):
        checks.append(1)
        if len(checks) >= 4:
            return {'status': 'unknown'}
        return original(approval_id, scope)
    monkeypatch.setattr(permission.client, 'check', revoke_at_last)
    result = h.flow.merge(item(), target(), safe, verified)
    assert result['reason'] == 'individual_approval_required'
    assert not h.github.merges and not system['executed']

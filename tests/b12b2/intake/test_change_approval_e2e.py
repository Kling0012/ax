"""Change component request, real HQ approval screen and intake decision."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.testclient import TestClient

from axcommon import hq_proof
from axcommon.change_approval import ChangeApprovalClient, component_proof, REQUEST_PATH
from core.hq.approval.intake import UnixIntake
from core.hq.approval.private import PrivateStore
from core.hq.approval.qr import svg
from core.hq.approval.security import Identity
from core.hq.approval.service import FactorService
from core.hq.change_approval import routes
from core.hq.ext.b12b2 import BrowserPolicy, register
from tests.b12b2.intake.test_hq_proof_e2e import system
from tests.fakes.b12b2.hq.parts import Forms, fake_checker

TOKEN = 'fixture-change-token'


def setup_apps(system, tmp_path, monkeypatch):
    import yaml
    from pathlib import Path
    config = Path(__file__).resolve().parents[3] / 'config/intake-destinations.d/b12b2.yaml'
    deny = next(entry for entry in yaml.safe_load(config.read_text()) if entry['name'] == 'approval.deny')
    system['app'].state.intake.registry._entries['approval.deny'] = deny
    monkeypatch.setenv('AX_WORK_SUBNET', '127.0.0.0/8')
    monkeypatch.setattr('core.hq.notify.receipt_auth.creds.read_secret_optional', lambda name: TOKEN)
    def relay(method, path, raw, headers=None):
        response = system['hq'].request(method, path, content=raw, headers=headers)
        return response.status_code, response.content
    internal = Starlette(routes=routes(transport=relay))
    internal.state.hq_settings = SimpleNamespace(sample=False)
    async def entry(scope, receive, send):
        scope['client'] = ('127.0.0.1', 1234)
        await internal(scope, receive, send)
    component = TestClient(entry, base_url='http://hq')
    def transport(method, url, headers, body, timeout):
        from urllib.parse import urlsplit
        response = component.request(method, urlsplit(url).path, headers=headers, content=body)
        return response.status_code, dict(response.headers), response.content
    client = ChangeApprovalClient(TOKEN, transport=transport)
    identity = Identity('fixture-user', 'fixture-session', True)
    private = tmp_path / 'hq-private'
    private.mkdir(mode=0o700)
    factor = FactorService(PrivateStore(private), checker=fake_checker, qr_renderer=svg,
                           emit=lambda _: None, notify=lambda _: None)
    factor.enroll(identity)
    app = Starlette()
    app.state.hq_approval_intake = UnixIntake(transport=relay, proof_encoder=hq_proof.encode)
    app.state.hq_approval_factor = factor
    app.state.hq_approval_identity = lambda request: identity
    app.state.hq_approval_browser_policy = BrowserPolicy(frozenset({'hq.test'}), frozenset({'https://hq.test'}))
    register(app)
    browser = TestClient(app, base_url='https://hq.test')
    return client, component, browser


def scope():
    return {'project': 'example', 'item': {'provider': 'github', 'issue_repo': 'owner/repo', 'kind': 'pr', 'number': 1},
            'repo': 'owner/repo', 'pr': 1, 'head_sha': 'a' * 40, 'input_revision': 'rev-1', 'operation': 'merge',
            'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}


def test_component_cannot_approve_but_person_can(system, tmp_path, monkeypatch):
    client, component, browser = setup_apps(system, tmp_path, monkeypatch)
    spec = scope()
    result = client.request('request-1', spec)
    aid = result['approval_id']
    assert result['status'] == 'pending_user'
    assert client.check(aid, spec)['status'] == 'pending'
    assert system['ledger'].get('/v1/todos/' + aid).json()['card']['approval_id'] == aid
    assert not system['executed']
    assert component.post('/v1/hq/approvals/' + aid + '/grant', json={}, headers={'Authorization': 'Bearer ' + TOKEN}).status_code == 404
    assert browser.post('/v1/change/approval-requests', json={}, headers={'Authorization': 'Bearer ' + TOKEN}).status_code == 404
    page = browser.get('/approvals/' + aid)
    assert page.status_code == 200 and 'owner/repo' in page.text and 'merge' in page.text and 'rev-1' in page.text
    form = Forms(page.text).values
    assert browser.post('/v1/hq/approvals/' + aid + '/grant', headers={'origin': 'https://hq.test'}, json={**form, 'code': '000000'}).status_code == 403
    page = browser.get('/approvals/' + aid)
    approved = browser.post('/v1/hq/approvals/' + aid + '/grant', headers={'origin': 'https://hq.test'},
                            json={**Forms(page.text).values, 'code': '123456'})
    assert approved.status_code == 200, approved.text
    assert client.check(aid, spec)['status'] == 'approved'
    assert client.check(aid, {**spec, 'input_revision': 'changed'})['status'] == 'unknown'
    assert not system['executed']


def test_scope_replay_and_proof_boundaries(system, tmp_path, monkeypatch):
    client, component, browser = setup_apps(system, tmp_path, monkeypatch)
    spec = scope()
    first = client.request('request-2', spec)
    assert client.request('request-2', spec) == first
    assert client.check(first['approval_id'], spec)['status'] == 'pending'
    from axcommon.client import ApiError
    import pytest
    with pytest.raises(ApiError) as error:
        client.request('request-2', {**spec, 'head_sha': 'b' * 40})
    assert error.value.status == 409
    signed = {'request_id': 'request-3', 'scope': spec}
    proof = component_proof(REQUEST_PATH, signed)
    assert system['hq'].post('/v1/instructions', json=signed, headers={'X-AX-Change-Proof': proof}).status_code >= 400
    assert system['hq'].post('/v1/approvals/' + first['approval_id'] + '/grant', json={'digest': 'x'},
                             headers={'X-AX-Change-Proof': proof}).status_code == 403
    monkeypatch.setattr('core.hq.notify.receipt_auth.creds.read_secret_optional', lambda name: 'other')
    assert client.check(first['approval_id'], spec)['status'] == 'unknown'
    assert not system['executed']


def test_deny_and_scope_changes_never_authorize(system, tmp_path, monkeypatch):
    client, _, browser = setup_apps(system, tmp_path, monkeypatch)
    spec = scope()
    aid = client.request('denial', spec)['approval_id']
    page = browser.get('/approvals/' + aid)
    denied = browser.post('/v1/hq/approvals/' + aid + '/deny', headers={'origin': 'https://hq.test'},
                          json={**Forms(page.text).values, 'code': '123456'})
    assert denied.status_code == 200, denied.text
    assert client.check(aid, spec)['status'] == 'denied'
    for changed in ({'repo': 'other/repo'}, {'head_sha': 'b' * 40}, {'pr': 2},
                    {'operation': 'pr_update'}, {'input_revision': 'rev-2'},
                    {'item': {**spec['item'], 'number': 2}},
                    {'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()}):
        assert client.check(aid, {**spec, **changed})['status'] == 'unknown'
    assert not system['executed']


def test_expiry_peer_and_component_proof_isolation(system, tmp_path, monkeypatch):
    from axcommon.client import ApiError
    import pytest
    client, component, _ = setup_apps(system, tmp_path, monkeypatch)
    spec = scope()
    aid = client.request('expiry', spec)['approval_id']
    monkeypatch.setenv('AX_WORK_SUBNET', '192.0.2.0/24')
    assert client.check(aid, spec)['status'] == 'unknown'
    assert component.post('/v1/change/approval-check', json={'approval_id': aid, 'scope': spec},
                          headers={'Authorization': 'Bearer ' + TOKEN}).status_code == 403
    monkeypatch.setenv('AX_WORK_SUBNET', '127.0.0.0/8')
    expired = {**spec, 'expires_at': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    with pytest.raises(ApiError) as err:
        client.request('expired', expired)
    assert err.value.status == 422
    for invalid in ({**spec, 'pr': None}, {**spec, 'head_sha': None}, {**spec, 'operation': 'unknown'}):
        with pytest.raises(ValueError):
            client.request('invalid', invalid)
    assert not system['executed']


def test_public_hq_rejects_component_endpoint(system, tmp_path, monkeypatch):
    from core.hq.app import create_app
    from core.hq.ext import b10a
    from core.hq.settings import Settings
    from tests.fakes.b10a.notify import Harness

    monkeypatch.setenv('AX_HQ_NOTIFY_RUN', '0')
    h = Harness(tmp_path / 'public-notify.sqlite')
    monkeypatch.setattr(b10a, 'build_hub', lambda **kwargs: h.hub)
    ext_dir = tmp_path / 'empty-ext'
    ext_dir.mkdir()
    settings = Settings(public_proxy_ip='127.0.0.1', origins=frozenset({'https://hq.test'}), ui_dir=tmp_path / 'none')
    app = create_app(settings=settings, authenticator=lambda request: {'principal': 'fixture-user', 'direct_tailnet': True},
                     secret_slots=object(), ext_dir=ext_dir, run_heartbeat_loop=False)
    b10a.register(app)
    browser = TestClient(app, base_url='https://hq.test')
    headers = {'x-forwarded-for': '100.64.0.10', 'origin': 'https://hq.test', 'authorization': 'Bearer ' + TOKEN}
    response = browser.post('/v1/change/approval-requests', json={'request_id': 'x', 'scope': scope()}, headers=headers)
    assert response.status_code in (403, 404)
    assert not system['app'].state.approvals.store.list()
    h.close()

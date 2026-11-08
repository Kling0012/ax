"""Exercise HQ assertions and durable cards through the intake and ledger APIs."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit

import pytest
import yaml
from starlette.testclient import TestClient

from axcommon.hq_proof import encode
from core.hq.approval.security import Identity, VerifiedAction
from core.intake.app import build_app
from core.intake.vias import Ident
from core.ledger import LedgerService
from core.ledger.app import create_core_app
from core.ledger.store import Store
from tests.fakes.b12b2.intake.parts import destination, instruction


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv('AX_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('AX_HQ_UID', str(2001))
    monkeypatch.setenv('AX_HQ_GID', str(2002))
    key = tmp_path / 'key'
    key.write_bytes(b'fictional-test-key-01234567890123456789')
    monkeypatch.setenv('AX_HQ_PROOF_KEY_FILE', str(key))
    token = 'fictional-ledger-credential-0123456789'
    token_path = tmp_path / 'token'
    token_path.write_text(token)
    monkeypatch.setenv('AX_CORE_TOKEN_FILE', str(token_path))
    token_table = tmp_path / 'core-tokens'
    token_table.write_text('intake ' + hashlib.sha256(token.encode()).hexdigest() + '\n')
    monkeypatch.setenv('AX_CORE_TOKENS', str(token_table))
    config = tmp_path / 'config'
    config.mkdir()
    routine = destination('ledger.complete', 'operate')
    routine['handler'] = 'internal:ledger.complete'
    (config / 'intake-destinations.yaml').write_text(yaml.safe_dump([destination(), routine, destination('permission.change'), destination('subloop.create', 'operate')]))
    ledger_store = Store(tmp_path / 'ledger.sqlite')
    ledger = TestClient(create_core_app(LedgerService(ledger_store, None, secret_scan=lambda _: [])))

    def transport(method, url, headers, body, timeout):
        response = ledger.request(method, urlsplit(url).path, headers=headers, content=body)
        return response.status_code, dict(response.headers), response.content

    app = build_app(config_dir=config, dest_dir=tmp_path / 'empty', transport=transport, run_heartbeat_loop=False)
    assert app.state.ext_results['b12b2'] is None
    assert any(f.__closure__ for f in app.state.intake.extra_listener_factories)
    app.state.intake.dest_handlers['ledger.complete'] = lambda app, dest, body, ident: (200, {'ok': True})
    executed = []
    real_executor = app.state.approvals.executor
    app.state.approvals.executor = lambda command: executed.append(command) or {'ok': True}

    def client(ident):
        async def entry(scope, receive, send):
            if scope['type'] == 'http' and ident:
                scope['ax_ident'] = ident
            await app(scope, receive, send)
        return TestClient(entry)

    yield {'local': client(Ident('local', 'commander', 'local-example', 'irreversible')),
           'hq': client(Ident('hq', 'hq', 'user', 'irreversible')), 'other': client(None),
           'ledger': ledger, 'executed': executed, 'key': key, 'app': app, 'real_executor': real_executor}
    ledger_store.close()


def signed(path, body, *, factor=True):
    _, headers = encode('POST', path, body, VerifiedAction(Identity('test-user', 'test-session', True), factor))
    return headers


def alter_claim(headers, key, transform):
    payload, _ = headers['X-AX-HQ-Proof'].split('.')
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    transform(claims)
    raw = json.dumps(claims, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    signature = hmac.new(key.read_bytes(), payload.encode(), hashlib.sha256).hexdigest()
    return {'X-AX-HQ-Proof': payload + '.' + signature}


def core_instruction(key='request-1'):
    payload = {'name': 'example-change', 'project': 'demo', 'title': 'Example', 'body': 'Example change',
               'acceptance': {'examples': ['works'], 'boundaries': ['rejects invalid']}}
    return instruction(payload=payload, key=key)


def test_hq_signed_approval_and_card_boundaries(system):
    body = {**core_instruction(), 'from': 'hq'}
    result = system['hq'].post('/v1/instructions', json=body, headers=signed('/v1/instructions', body, factor=False))
    assert result.status_code == 202, result.text
    aid = result.json()['approval_id']
    card = system['ledger'].get('/v1/todos/' + aid)
    assert card.status_code == 200
    assert card.json()['card']['approval_id'] == aid
    assert card.json()['state'] == 'waiting_user'
    assert not system['executed']
    digest = card.json()['card']['approval']['data']['digest']
    path = '/v1/approvals/' + aid + '/grant'
    body = {'digest': digest}
    assert system['hq'].post(path, json={}).status_code == 422
    assert system['hq'].post(path, json=body).status_code == 403
    valid = signed(path, body)
    forged = {'X-AX-HQ-Proof': valid['X-AX-HQ-Proof'][:-1] + ('0' if valid['X-AX-HQ-Proof'][-1] != '0' else '1')}
    assert system['hq'].post(path, json=body, headers=forged).status_code == 403
    assert system['hq'].post(path, json={'digest': 'forged'}, headers=valid).status_code == 403
    expired = alter_claim(valid, system['key'], lambda c: c.update(iat=int(time.time()) - 61, exp=int(time.time()) - 1))
    assert system['hq'].post(path, json=body, headers=expired).status_code == 403
    assert system['other'].post(path, json=body, headers=valid).status_code == 403
    assert system['hq'].post(path, json=body, headers=signed(path, body, factor=False)).status_code == 403
    assert not system['executed']
    ok = system['hq'].post(path, json=body, headers=valid)
    assert ok.status_code == 200, ok.text
    assert ok.json()['data']['granted_by'] == 'test-user'
    assert len(system['executed']) == 1
    assert system['hq'].post(path, json=body, headers=valid).status_code == 403
    assert len(system['executed']) == 1


def test_hq_browser_to_ledger_and_intake(system, tmp_path):
    from starlette.applications import Starlette
    from core.hq.approval.intake import UnixIntake
    from core.hq.approval.private import PrivateStore
    from core.hq.approval.qr import svg
    from core.hq.approval.service import FactorService
    from core.hq.ext.b12b2 import BrowserPolicy, register
    from tests.fakes.b12b2.hq.parts import Forms, fake_checker

    body = {**core_instruction(key='browser'), 'from': 'hq'}
    pending = system['hq'].post('/v1/instructions', json=body, headers=signed('/v1/instructions', body, factor=False))
    assert pending.status_code == 202, pending.text
    aid = pending.json()['approval_id']
    identity = Identity('test-user', 'test-session', True)
    private = tmp_path / 'hq-private'
    private.mkdir(mode=0o700)
    notices = []
    factor = FactorService(PrivateStore(private), checker=fake_checker, qr_renderer=svg, emit=lambda _: None, notify=notices.append)
    factor.enroll(identity)
    assert notices
    assert not system['executed']
    assert system['ledger'].get('/v1/todos/' + aid).json()['state'] == 'waiting_user'

    def relay(method, path, raw, headers=None):
        response = system['hq'].request(method, path, content=raw, headers=headers)
        return response.status_code, response.content

    app = Starlette()
    app.state.hq_approval_intake = UnixIntake(transport=relay, proof_encoder=encode)
    app.state.hq_approval_factor = factor
    app.state.hq_approval_identity = lambda request: identity
    app.state.hq_approval_browser_policy = BrowserPolicy(frozenset({'hq.test'}), frozenset({'https://hq.test'}))
    register(app)
    browser = TestClient(app, base_url='https://hq.test')
    page = browser.get('/approvals/' + aid)
    assert page.status_code == 200, page.text
    assert not system['executed']
    result = browser.post('/v1/hq/approvals/' + aid + '/grant',
                          headers={'origin': 'https://hq.test'},
                          json={**Forms(page.text).values, 'code': '123456'})
    assert result.status_code == 200, result.text
    assert len(system['executed']) == 1
    assert system['ledger'].get('/v1/todos/' + aid).json()['user_only'] is True


def test_hq_one_request_two_verifiers_and_missing_key(system, monkeypatch, tmp_path):
    path = '/v1/instructions'
    body = {**instruction('ledger.complete', {'todo_id': 'example'}, key='once'), 'from': 'hq'}
    monkeypatch.setenv('AX_HQ_PROOF_KEY_FILE', str(tmp_path / 'missing'))
    reply = system['hq'].post(path, json=body)
    assert reply.status_code == 200, reply.text
    pending_body = {**core_instruction(key='missing'), 'from': 'hq'}
    pending = system['hq'].post(path, json=pending_body)
    assert pending.status_code == 202, pending.text
    aid = pending.json()['approval_id']
    card = system['ledger'].get('/v1/todos/' + aid).json()
    assert card['state'] == 'waiting_user'
    assert not system['executed']
    grant = system['hq'].post('/v1/approvals/' + aid + '/grant',
                              json={'digest': card['card']['approval']['data']['digest']})
    assert grant.status_code == 503
    assert grant.json()['error']['code'] == 'proof_key_unavailable'
    assert system['hq'].get('/v1/approvals/' + aid).status_code == 200
    assert system['local'].get('/v1/destinations').status_code == 200
    assert not system['executed']


def test_factor_adapter_enrollment_does_not_approve(system, tmp_path):
    from starlette.applications import Starlette
    from core.hq.approval.private import PrivateStore
    from core.hq.ext.b12b2 import BrowserPolicy, register
    from tests.fakes.b12b2.hq.parts import FakeFactorAdapters, Forms

    body = {**core_instruction(key='factor-event'), 'from': 'hq'}
    pending = system['hq'].post('/v1/instructions', json=body)
    assert pending.status_code == 202, pending.text
    aid = pending.json()['approval_id']
    root = tmp_path / 'private-parent'
    root.mkdir(mode=0o700)
    adapters = FakeFactorAdapters()
    app = Starlette()
    app.state.hq_factor_private_store = PrivateStore(root / 'private')
    app.state.hq_factor_adapters = adapters
    app.state.hq_approval_identity = lambda request: Identity('test-user', 'test-session', True)
    app.state.hq_approval_browser_policy = BrowserPolicy(frozenset({'hq.test'}), frozenset({'https://hq.test'}))
    register(app)
    browser = TestClient(app, base_url='https://hq.test')
    page = browser.get('/approval/settings')
    assert page.status_code == 200, page.text
    result = browser.post('/v1/hq/approval/enroll', headers={'origin': 'https://hq.test'},
                          json=Forms(page.text).values)
    assert result.status_code == 200, result.text
    assert len(adapters.events) == len(adapters.notifications) == 1
    assert {item['kind'] for item in adapters.notifications.values()} == {'approval.factor.enrolled'}
    assert system['ledger'].get('/v1/todos/' + aid).json()['state'] == 'waiting_user'
    assert system['hq'].get('/v1/approvals/' + aid).json()['data']['state'] == 'pending_user'
    assert not system['executed']


def test_hq_permission_change_requires_bound_proof(system, monkeypatch, tmp_path):
    path = '/v1/instructions'
    body = {**instruction('permission.change', {'project': 'demo', 'repo': 'demo', 'destination': 'demo', 'operations': ['merge'], 'source_instruction': 'example'}, key='permission'), 'from': 'hq'}
    assert system['hq'].post(path, json=body).status_code == 403
    headers = signed(path, body, factor=False)
    response = system['hq'].post(path, json=body, headers=headers)
    assert response.status_code == 202, response.text
    approval = system['hq'].get('/v1/approvals/' + response.json()['approval_id'])
    assert approval.json()['data']['requested_by'] == 'test-user'
    assert system['hq'].post(path, json=body, headers=headers).status_code == 403
    assert not system['executed']
    monkeypatch.setenv('AX_HQ_PROOF_KEY_FILE', str(tmp_path / 'missing'))
    response = system['hq'].post(path, json={**body, 'idempotency_key': 'missing'})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'proof_key_unavailable'


def test_hq_screen_reports_missing_key_without_blocking_views(system, monkeypatch, tmp_path):
    from starlette.applications import Starlette
    from core.hq.approval.intake import UnixIntake
    from core.hq.approval.private import PrivateStore
    from core.hq.approval.qr import svg
    from core.hq.approval.service import FactorService
    from core.hq.ext.b12b2 import BrowserPolicy, register
    from tests.fakes.b12b2.hq.parts import Forms, fake_checker

    body = {**core_instruction(key='missing-key-screen'), 'from': 'hq'}
    pending = system['hq'].post('/v1/instructions', json=body)
    assert pending.status_code == 202
    aid = pending.json()['approval_id']
    identity = Identity('test-user', 'test-session', True)
    root = tmp_path / 'hq-private-parent'
    root.mkdir(mode=0o700)
    factor = FactorService(PrivateStore(root / 'private'), checker=fake_checker, qr_renderer=svg,
                           emit=lambda _: None, notify=lambda _: None)
    factor.enroll(identity)

    def relay(method, path, raw, headers=None):
        response = system['hq'].request(method, path, content=raw, headers=headers)
        return response.status_code, response.content

    app = Starlette()
    app.state.hq_approval_intake = UnixIntake(transport=relay, proof_encoder=encode)
    app.state.hq_approval_factor = factor
    app.state.hq_approval_identity = lambda request: identity
    app.state.hq_approval_browser_policy = BrowserPolicy(frozenset({'hq.test'}), frozenset({'https://hq.test'}))
    register(app)
    browser = TestClient(app, base_url='https://hq.test')
    monkeypatch.setenv('AX_HQ_PROOF_KEY_FILE', str(tmp_path / 'missing'))
    page = browser.get('/approvals/' + aid)
    assert page.status_code == 200, page.text
    response = browser.post('/v1/hq/approvals/' + aid + '/grant',
                            headers={'origin': 'https://hq.test'},
                            json={**Forms(page.text).values, 'code': '123456'})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'proof_key_unavailable'
    assert not system['executed']
    assert system['ledger'].get('/v1/todos/' + aid).json()['state'] == 'waiting_user'


def test_signed_subloop_approval_replays_original_request_once(system, tmp_path):
    from pathlib import Path
    from axcommon import schemas
    from tests.fakes.b12.subloop.world import World
    from core.intake.dest.b12b3.subloop import register

    app = system['app']
    app.state.seats.local_tier = lambda ident: ident
    world = World(tmp_path, Path(__file__).resolve().parents[3])
    register(app, world.service)
    app.state.approvals.executor = system['real_executor']
    app.state.approvals.policy.schema_loader = lambda name: (schemas.load(name.removeprefix('schemas/'))
                                                               if name.endswith('subloop.create.json') else schema_for(name))
    payload = {'plugin_id': 'new-one', 'project': 'demo', 'title': 'Create', 'body': 'Create a loop',
               'spec': {'secrets': [], 'outputs': ['external'], 'sensitivity': 'private'},
               'acceptance': {'examples': ['works'], 'boundaries': ['no surprises']}, 'quote': 'Create a loop'}
    request = instruction('subloop.create', payload, key='original-request')
    pending = system['local'].post('/v1/instructions', json=request)
    assert pending.status_code == 202, pending.text
    aid = pending.json()['approval_id']
    card = system['ledger'].get('/v1/todos/' + aid).json()
    body = {'digest': card['card']['approval']['data']['digest']}
    path = '/v1/approvals/' + aid + '/grant'
    assert not world.items
    assert system['hq'].post(path, json=body).status_code == 403
    proof = signed(path, body)
    forged = {'X-AX-HQ-Proof': proof['X-AX-HQ-Proof'][:-1] + ('0' if proof['X-AX-HQ-Proof'][-1] != '0' else '1')}
    assert system['hq'].post(path, json=body, headers=forged).status_code == 403
    expired = alter_claim(proof, system['key'], lambda c: c.update(iat=int(time.time()) - 61, exp=int(time.time()) - 1))
    assert system['hq'].post(path, json=body, headers=expired).status_code == 403
    assert not world.items
    result = system['hq'].post(path, json=body, headers=proof)
    assert result.status_code == 200, result.text
    assert len(world.items) == 1
    assert world.todos[0]['request_id'] == 'original-request'
    assert world.todos[0]['created_by'] == 'local-example'
    assert world.todos[0]['approval_id'] == aid
    assert system['hq'].post(path, json=body, headers=proof).status_code == 403
    assert system['hq'].post(path, json=body, headers=signed(path, body)).status_code == 409
    assert len(world.items) == 1

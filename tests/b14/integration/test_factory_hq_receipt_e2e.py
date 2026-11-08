"""The standard change factory talks to the real HQ receipt ASGI app over HTTP."""
import _socket
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn

from core.hq.ext.b10a import receipt_app
from axcommon.client import ApiError
from plugins.subloop.change.integration.gate import MergeGate
from plugins.subloop.change.integration.runtime import build_executor
from runtime.subloop.tools import GitHubClient
from tests.fakes.b7 import make_env
from tests.fakes.b10a.notify import Harness
from tests.fakes.b14.integration import HEAD, Permission, Safety, Verification, item, snapshot, target
from tests.b14.integration.test_standard_factory_e2e import execute, server

TOKEN = 'fixture-plugin-token'


@pytest.fixture
def connected(tmp_path, repo_root, fake_clock, monkeypatch, server):
    github_url, github_state = server
    hq = Harness(tmp_path / 'hq.sqlite')
    monkeypatch.setenv('AX_WORK_SUBNET', '127.0.0.0/8')
    monkeypatch.setenv('AX_HQ_NOTIFY_RUN', '1')
    monkeypatch.setattr('core.hq.notify.receipt_auth.creds.read_secret_optional', lambda name: TOKEN)
    app = receipt_app(hq.hub, SimpleNamespace(sample=False))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(app, proxy_headers=False, lifespan='off', access_log=False, log_level='error'))
    thread = threading.Thread(target=uv.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if uv.started:
            break
        time.sleep(.01)
    assert uv.started
    github_port = int(github_url.rsplit(':', 1)[1])
    def connect(sock, address):
        if sock.family != socket.AF_INET or address not in {('127.0.0.1', port), ('127.0.0.1', github_port)}:
            raise RuntimeError('external network forbidden')
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setenv('AX_CHANGE_HQ_URL', f'http://127.0.0.1:{port}')
    monkeypatch.setenv('AX_CHANGE_RELEASE_URL', github_url)
    settings = {'issue_repo': 'owner/code', 'implementation_repo': 'owner/code', 'merge_method': 'merge',
                'ci_sources': [], 'test_recipe': 'fake', 'priority_labels': [], 'execution_cap': 1,
                'allowed_paths': ['**'], 'untrusted_forbidden_paths': ['.github/**', 'package.json', 'Dockerfile*'],
                'trusted_authors': ['trusted'], 'outbound_allowlist': []}
    targets = tmp_path / 'targets.yaml'
    targets.write_text(json.dumps({'targets': [settings]}))
    monkeypatch.setenv('AX_CHANGE_TARGETS_FILE', str(targets))
    (tmp_path / 'b7').mkdir()
    env = make_env(tmp_path / 'b7', repo_root, fake_clock)
    env.svc.plugin_dir = repo_root / 'plugins/subloop/change'
    env.svc.github = GitHubClient('fixture-github-token', base_url=github_url)
    env.svc.ledger = SimpleNamespace(_token=TOKEN)
    ex, flow, errors = build_executor(root=repo_root, services=env.svc, environ={
        'AX_DATA_DIR': str(tmp_path / 'data'), 'AX_CHANGE_TARGETS_FILE': str(targets),
        'AX_CHANGE_PORT_FACTORY': 'plugins.subloop.change.integration.factory:create_ports'})
    assert not errors
    yield SimpleNamespace(ex=ex, flow=flow, hq=hq, github=github_state)
    ex.store.close()
    ex.change_journal.close()
    uv.should_exit = True
    thread.join(5)
    hq.close()


def test_collect_only_graph_has_no_hq_delivery(connected):
    c = connected
    execute(c.ex)
    assert c.flow.ports.collection.store.rows(), (c.ex.mode, c.github["calls"], [(e["kind"],e.get("data")) for e in c.flow.journal.events()])
    assert c.hq.world.ntfy.sent == []
    assert c.hq.store._db.execute('SELECT COUNT(*) FROM delivery_receipts').fetchone()[0] == 0
    api = c.flow.ports.notifications.backend.api
    result = api.post('/v1/notify/deliveries', {'delivery_id': 'collect', 'destination': 'user',
                                               'body': {'message': 'fixture'}, 'mode': 'collect_only'})
    assert result['status'] == 'unknown'
    assert c.hq.store.receipt_get('collect') is None
    assert not c.hq.world.ntfy.sent
    assert not any(entry[0] != 'GET' for entry in c.github['calls'])


def test_factory_accepted_starts_wait_and_no_duplicate(connected):
    c = connected
    body = {'repo': 'owner/code', 'pr': 1, 'head_sha': HEAD, 'instruction_ref': 'instruction-1'}
    result = c.flow.notices.deliver('notice-e2e', 'user', body)
    assert result['status'] == 'unknown'
    assert result['accepted'] is True and isinstance(result['accepted_at'], (int, float))
    assert 'delivered_at' not in result
    assert len(c.hq.world.ntfy.sent) == 1
    assert c.flow.ports.notifications.reconcile('notice-e2e', 'user')['status'] == 'unknown'
    assert c.flow.notices.deliver('notice-e2e', 'user', {**body, 'pr': 2})['reason'] == 'delivery_identity_mismatch'
    assert c.flow.ports.notifications.send('notice-e2e', 'user', body)['status'] == 'unknown'
    api = c.flow.ports.notifications.backend.api
    for payload in ({**body, 'pr': 2}, body):
        destination = 'user' if payload != body else 'other'
        with pytest.raises(ApiError) as err:
            api.post('/v1/notify/deliveries', {'delivery_id': 'notice-e2e', 'destination': destination,
                                                'body': payload, 'mode': 'run'})
        assert err.value.status == 409
    assert len(c.hq.world.ntfy.sent) == 1
    request = item(trusted=False)
    live = snapshot(request)
    safety = Safety().review({**request, 'snapshot': live})
    verification = Verification().verify(request, target(), head_sha=HEAD, base_sha=live['base_sha'])
    permissions = Permission()
    permissions.authorize(request)
    notice = {**result, 'head_sha': HEAD, 'instruction_ref': 'instruction-1'}
    def evaluate(candidate, now):
        return MergeGate().evaluate(request, target(), live, safety, verification, {'allowed': True},
                                    now=now, instruction=permissions.grant, notification=candidate)
    at = result['accepted_at']
    assert evaluate(notice, at + 1799).reason == 'merge_notice_wait'
    assert evaluate(notice, at + 1800).allowed
    assert evaluate({**notice, 'accepted': False}, at + 1800).reason == 'merge_notice_required'
    assert not evaluate({**notice, 'accepted_at': at + 1801}, at + 1800).allowed
    assert not evaluate({**notice, 'head_sha': 'b' * 40}, at + 1800).allowed
    assert not evaluate({**notice, 'instruction_ref': 'other'}, at + 1800).allowed


def test_factory_rejects_peer_token_and_hq_disabled(connected, monkeypatch):
    c = connected
    body = {'message': 'fixture'}
    api = c.flow.ports.notifications.backend.api
    monkeypatch.setenv('AX_WORK_SUBNET', '192.0.2.0/24')
    with pytest.raises(ApiError) as err:
        api.post('/v1/notify/deliveries/lookup', {'delivery_id': 'bad-peer', 'destination': 'user'})
    assert err.value.status == 403
    assert c.flow.ports.notifications.send('bad-peer', 'user', body)['status'] == 'unknown'
    assert c.hq.store.receipt_get('bad-peer') is None
    monkeypatch.setenv('AX_WORK_SUBNET', '127.0.0.0/8')
    monkeypatch.setattr('core.hq.notify.receipt_auth.creds.read_secret_optional', lambda name: 'other-token')
    with pytest.raises(ApiError) as err:
        api.post('/v1/notify/deliveries/lookup', {'delivery_id': 'bad-token', 'destination': 'user'})
    assert err.value.status == 401
    assert c.flow.ports.notifications.send('bad-token', 'user', body)['status'] == 'unknown'
    assert c.hq.store.receipt_get('bad-token') is None
    monkeypatch.setattr('core.hq.notify.receipt_auth.creds.read_secret_optional', lambda name: TOKEN)
    monkeypatch.setenv('AX_HQ_NOTIFY_RUN', '0')
    assert c.flow.ports.notifications.send('disabled', 'user', body)['status'] == 'unknown'
    assert c.hq.store.receipt_get('disabled') is None
    assert not c.hq.world.ntfy.sent

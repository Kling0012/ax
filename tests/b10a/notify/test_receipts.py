"""Receipt API exercised through the HQ boundary with an in-memory sender."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette

from core.hq.app import HQBoundary
from core.hq.ext import b10a
from core.hq.notify.policy import DestinationPolicy
from tests.fakes.b10a.asgi import call
from tests.fakes.b10a.notify import Harness

TOKEN = 'fixture-component-token'
HEADERS = {'authorization': 'Bearer ' + TOKEN}
SEND = '/v1/notify/deliveries'
LOOKUP = SEND + '/lookup'


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv('AX_HQ_NOTIFY_RUN', '1')
    monkeypatch.setattr('core.hq.notify.receipt_auth.creds.read_secret_optional', lambda name: TOKEN)
    h = Harness(tmp_path / 'notify.sqlite')
    settings = SimpleNamespace(sample=False, public_proxy_ip='127.0.0.1')
    app = b10a.receipt_app(h.hub, settings)
    async def unix_app(scope, receive, send):
        scope["client"] = ('198.51.100.5', 1234)
        await app(scope, receive, send)
    unix_app.inner = unix_app
    unix_app.raw_app = app
    unix_app.state = app.state
    yield h, unix_app
    h.close()


def send(app, ident='a', destination='user', body=None, mode='run', headers=HEADERS):
    return call(app, 'POST', SEND, body={'delivery_id': ident, 'destination': destination,
                                       'body': body or {'message': 'fixture'}, 'mode': mode}, headers=headers)


def lookup(app, ident='a', destination='user'):
    return call(app, 'POST', LOOKUP, body={'delivery_id': ident, 'destination': destination}, headers=HEADERS)


def test_concurrent_replay_and_restart(fixture, tmp_path):
    h, app = fixture
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: send(app), range(12)))
    assert all(status == 200 and result['status'] == 'unknown' for status, result in results)
    assert len(h.world.ntfy.sent) == 1
    assert 'delivered_at' not in results[0][1]
    accepted_at = h.clock().timestamp()
    assert all(result['accepted'] and result['accepted_at'] == accepted_at for _, result in results)
    assert h.store.receipt_get('a')['accepted_at'] == accepted_at
    h.clock.advance(minutes=5)
    h.store.close()
    from core.hq.notify.store import NotifyStore
    h.store = h.dispatcher.store = h.hub.store = NotifyStore(tmp_path / 'notify.sqlite')
    for result in (lookup(app)[1], send(app)[1]):
        assert result['status'] == 'unknown'
        assert result['accepted'] is True and result['accepted_at'] == accepted_at
        assert 'delivered_at' not in result and 'provider_id' not in result
    collected = send(app, mode='collect_only')[1]
    assert collected['accepted'] is False and collected['accepted_at'] is None
    conflict, data = send(app, body={'message': 'different'})
    assert conflict == 409 and data['accepted'] is False and data['accepted_at'] is None
    assert len(h.world.ntfy.sent) == 1


def test_lost_response_and_conflicts(fixture):
    h, app = fixture
    original = h.sender.send
    def lose(**kwargs):
        original(**kwargs)
        raise ConnectionError('response lost')
    h.sender.send = lose
    for result in (send(app)[1], lookup(app)[1], send(app)[1]):
        assert result['status'] == 'unknown'
        assert result['accepted'] is False and result['accepted_at'] is None
    assert len(h.world.ntfy.sent) == 1
    assert send(app, body={'message': 'different'})[0] == 409
    assert send(app, destination='other')[0] == 409
    assert lookup(app, destination='other')[0] == 409


def test_collect_rate_guard_and_component_boundary(fixture, monkeypatch):
    h, app = fixture
    collected = send(app, mode='collect_only')[1]
    assert collected['status'] == 'unknown'
    assert collected['accepted'] is False and collected['accepted_at'] is None
    assert not h.world.ntfy.sent
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 0)
    result = send(app)[1]
    assert result['status'] == 'retry' and result['next_retry'] > h.clock().timestamp()
    assert result['accepted'] is False and result['accepted_at'] is None
    assert not h.world.ntfy.sent
    assert send(app, ident='x', headers={})[0] == 401
    assert send(app, ident='x', headers={**HEADERS, 'origin': 'https://fixture.invalid'})[0] == 403
    assert send(app, ident='x', headers={**HEADERS, 'x-forwarded-for': '100.64.0.1'})[0] == 403
    monkeypatch.setenv('AX_HQ_NOTIFY_RUN', '0')
    off = send(app, ident='off')[1]
    assert off['status'] == 'unknown' and off['accepted'] is False and off['accepted_at'] is None
    assert not h.world.ntfy.sent



def test_retry_then_accept_and_require_provider_id(fixture):
    from core.hq.notify.ntfy import Receipt

    h, app = fixture
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 0)
    first = send(app)[1]
    assert first['status'] == 'retry' and first['accepted'] is False
    h.clock.advance(seconds=61)
    h.dispatcher.policies['ntfy'] = DestinationPolicy('ntfy', 10)
    accepted = send(app)[1]
    assert accepted['accepted'] is True and accepted['accepted_at'] == h.clock().timestamp()
    assert accepted['status'] == 'unknown' and 'delivered_at' not in accepted
    assert len(h.world.ntfy.sent) == 1

    h.sender.send = lambda **kwargs: Receipt(True, id=None)
    missing = send(app, ident='missing-id')[1]
    assert missing['status'] == 'unknown' and missing['accepted'] is False
    assert missing['accepted_at'] is None
    assert lookup(app, ident='missing-id')[1]['accepted'] is False
    h.sender.send = lambda **kwargs: Receipt(False, id='untrusted', transient=True)
    failed = send(app, ident='failed')[1]
    assert failed['status'] == 'unknown' and failed['accepted'] is False
    assert failed['accepted_at'] is None


def test_incomplete_saved_provider_evidence_is_not_accepted(fixture):
    h, app = fixture
    send(app)
    with h.store._lock:
        h.store._db.execute("UPDATE delivery_receipts SET provider_id='' WHERE delivery_id='a'")
    result = lookup(app)[1]
    assert result['status'] == 'unknown'
    assert result['accepted'] is False and result['accepted_at'] is None
    with h.store._lock:
        h.store._db.execute("UPDATE delivery_receipts SET provider_id='fixture', accepted_at=NULL WHERE delivery_id='a'")
    assert lookup(app)[1]['accepted'] is False


def test_real_http_unix_socket_boundary(fixture):
    import asyncio
    import json
    import socket
    import threading
    import uvicorn
    from uvicorn.protocols.http.h11_impl import H11Protocol
    from uvicorn.server import ServerState

    h, app = fixture
    config = uvicorn.Config(app.inner, proxy_headers=False, lifespan='off', access_log=False, log_level='error')
    config.load()

    def request(headers):
        client, server = socket.socketpair()
        ready = threading.Event()
        async def serve():
            loop = asyncio.get_running_loop()
            transport, _ = await loop.connect_accepted_socket(lambda: H11Protocol(config, ServerState(), {}), server)
            ready.set()
            await asyncio.sleep(.1)
            transport.close()
        thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
        thread.start()
        assert ready.wait(2)
        body = json.dumps({'delivery_id': 'real', 'destination': 'user', 'body': {'message': 'fixture'}, 'mode': 'run'}).encode()
        raw_headers = ''.join(f'{k}: {v}\r\n' for k, v in headers.items())
        client.sendall((f'POST {SEND} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n{raw_headers}Connection: close\r\n\r\n').encode() + body)
        response = b''
        while chunk := client.recv(65536):
            response += chunk
        client.close()
        thread.join(2)
        head, data = response.split(b'\r\n\r\n', 1)
        return int(head.split(b' ')[1]), json.loads(data)

    assert request({**HEADERS, 'x-forwarded-for': '100.64.0.1'})[0] == 403
    assert request({**HEADERS, 'forwarded': 'for=100.64.0.1'})[0] == 403
    app.state.hq_settings.sample = True
    assert request(HEADERS)[0] == 403
    app.state.hq_settings.sample = False
    assert request(HEADERS)[1]['status'] == 'unknown'
    assert len(h.world.ntfy.sent) == 1


def test_public_app_has_no_component_route(fixture, monkeypatch):
    h, _ = fixture
    monkeypatch.setenv('AX_HQ_NOTIFY_RUN', '0')
    monkeypatch.setattr(b10a, 'build_hub', lambda **kwargs: h.hub)
    app = Starlette()
    b10a.register(app)
    assert not any(getattr(route, 'path', '').startswith(SEND) for route in app.router.routes)
    assert call(app, 'POST', SEND, body={}, headers=HEADERS)[0] == 404


def test_internal_tcp_http(fixture, monkeypatch):
    import _socket
    import http.client
    import socket
    import threading
    import time
    import uvicorn

    h, wrapped = fixture
    monkeypatch.setenv('AX_WORK_SUBNET', '127.0.0.0/8')
    def local_connect(sock, address):
        if sock.family != socket.AF_INET or address[0] != '127.0.0.1':
            raise RuntimeError('only test loopback is allowed')
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(wrapped.raw_app, proxy_headers=False, lifespan='off', access_log=False, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(.01)
    assert server.started
    try:
        import json
        def request(path, headers):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            body = json.dumps({'delivery_id': 'tcp', 'destination': 'user', 'body': {'message': 'fixture'}, 'mode': 'run'})
            conn.request('POST', path, body, headers={'content-type': 'application/json', **headers})
            response = conn.getresponse()
            data = response.read()
            conn.close()
            return response.status, json.loads(data) if data and response.getheader("content-type", "").startswith("application/json") else None
        assert request(SEND, {**HEADERS, 'x-forwarded-for': '198.51.100.5'})[0] == 403
        assert request('/v1/notify/status', HEADERS)[0] == 404
        status, body = request(SEND, HEADERS)
        assert status == 200 and body['status'] == 'unknown'
        assert len(h.world.ntfy.sent) == 1
    finally:
        server.should_exit = True
        thread.join(5)


def test_guard_and_missing_token_fail_closed(fixture, monkeypatch):
    h, app = fixture
    h.dispatcher.guard.redactor = None
    result = send(app, ident='blocked')
    assert result[1]['status'] == 'blocked'
    assert not h.world.ntfy.sent
    monkeypatch.setattr('core.hq.notify.receipt_auth.creds.read_secret_optional', lambda name: None)
    assert send(app, ident='untrusted')[0] == 503
    assert not h.world.ntfy.sent


def test_public_hq_boundary_rejects_component_over_real_unix(fixture):
    import asyncio
    import socket
    import threading
    import uvicorn
    from uvicorn.protocols.http.h11_impl import H11Protocol
    from uvicorn.server import ServerState

    _, wrapped = fixture
    app = Starlette()
    app.state.hq_settings = wrapped.state.hq_settings
    app.state.hq_auth = lambda request: None
    app.add_middleware(HQBoundary, owner=app)
    config = uvicorn.Config(app, proxy_headers=False, lifespan='off', access_log=False, log_level='error')
    config.load()
    client, server = socket.socketpair()
    ready = threading.Event()
    async def serve():
        loop = asyncio.get_running_loop()
        transport, _ = await loop.connect_accepted_socket(lambda: H11Protocol(config, ServerState(), {}), server)
        ready.set()
        await asyncio.sleep(.1)
        transport.close()
    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    assert ready.wait(2)
    body = b'{}'
    client.sendall((f'POST {SEND} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {TOKEN}\r\nX-Forwarded-For: 127.0.0.1\r\nContent-Length: 2\r\nConnection: close\r\n\r\n').encode() + body)
    response = b''
    while chunk := client.recv(655655):
        response += chunk
    client.close()
    thread.join(2)
    assert response.startswith(b'HTTP/1.1 403 ')

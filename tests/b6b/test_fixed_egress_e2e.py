"""Socket-level tests for the fixed verification proxy policy."""
import asyncio
import _socket
import base64
import socket

import pytest

from tests.b6b.test_egress_proxy import EP


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def auth(job):
    return base64.b64encode(f'job-{job}:'.encode()).decode()


def test_fixed_proxy_over_real_sockets(tmp_path, monkeypatch):
    # The suite forbids outbound connections; permit loopback only in this test.
    def local_connect(sock, address):
        if not isinstance(address, tuple) or address[0] != '127.0.0.1':
            raise RuntimeError('only loopback is permitted')
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    async def scenario():
        allowed = tmp_path / 'allow'
        allowed.write_text('allowed.example\ninternal.example\nipv6.example\nmapped.example\nmixed.example\n')
        # A forged job identity must never select this unrelated file.
        (tmp_path / 'job-other.allow').write_text('other.example\n')
        proxy_port, gateway_port, health_port = port(), port(), port()
        env = {'EGRESS_FIXED_ALLOW_FILE': str(allowed), 'EGRESS_ALLOW_DIR': str(tmp_path),
               'EGRESS_LISTEN_HOST': '127.0.0.1', 'EGRESS_PROXY_PORT': str(proxy_port),
               'EGRESS_GATEWAY_PORT': str(gateway_port), 'AX_HEALTH_PORT': str(health_port)}
        ctx = EP.context_from_env(env)
        requests = []

        async def upstream(reader, writer):
            requests.append(await reader.read(100))
            writer.write(b'HTTP/1.1 302 Found\r\nLocation: https://other.example/\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            writer.close()

        upstream_server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        upstream_port = upstream_server.sockets[0].getsockname()[1]

        async def resolve(host, target_port):
            return {'internal.example': ['127.0.0.1'], 'ipv6.example': ['2606:4700:4700::1111'],
                    'mapped.example': ['::ffff:127.0.0.1'],
                    'mixed.example': ['1.1.1.1', '::1']}.get(host, ['1.1.1.1'])

        async def connect(host, target_port):
            return await asyncio.open_connection('127.0.0.1', upstream_port)

        ctx.resolver, ctx.connector = resolve, connect
        server = asyncio.create_task(EP.serve(ctx, env))
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', proxy_port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(.01)
        else:
            pytest.fail('proxy did not start')

        async def request(host, target_port=443, job=None, body=b''):
            reader, writer = await asyncio.open_connection('127.0.0.1', proxy_port)
            header = f'CONNECT {host}:{target_port} HTTP/1.1\r\nHost: {host}\r\n'
            if job:
                header += f'Proxy-Authorization: Basic {auth(job)}\r\n'
            writer.write(header.encode() + b'\r\n' + body)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), 3)
            writer.close()
            await writer.wait_closed()
            return data

        try:
            result = await request('allowed.example', body=b'GET / HTTP/1.0\r\n\r\n')
            assert result.startswith(b'HTTP/1.1 200 ')
            assert b'Location: https://other.example/' in result
            assert requests == [b'GET / HTTP/1.0\r\n\r\n']
            assert (await request('other.example', job='other')).startswith(b'HTTP/1.1 403 ')
            assert (await request('allowed.example', 3425, job='other')).startswith(b'HTTP/1.1 403 ')
            assert (await request('unlisted.example')).startswith(b'HTTP/1.1 403 ')
            assert (await request('internal.example')).startswith(b'HTTP/1.1 403 ')
            assert (await request('mapped.example')).startswith(b'HTTP/1.1 403 ')
            assert (await request('mixed.example')).startswith(b'HTTP/1.1 403 ')
            assert (await request('ipv6.example', body=b'GET / HTTP/1.0\r\n\r\n')).startswith(b'HTTP/1.1 200 ')
            # Changing a mounted file cannot switch this already-running policy.
            allowed.write_text('other.example\n')
            assert (await request('other.example', job='other')).startswith(b'HTTP/1.1 403 ')
            for closed_port in (gateway_port, health_port):
                with pytest.raises(OSError):
                    await asyncio.open_connection('127.0.0.1', closed_port)
        finally:
            server.cancel()
            with pytest.raises(asyncio.CancelledError):
                await server
            upstream_server.close()
            await upstream_server.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize('contents', ['@gate 8931\n', '*.example\n', '127.0.0.1\n', 'bad:443\n'])
def test_fixed_proxy_rejects_invalid_policy_at_start(tmp_path, contents):
    policy = tmp_path / 'policy'
    policy.write_text(contents)
    with pytest.raises(SystemExit):
        EP.context_from_env({'EGRESS_FIXED_ALLOW_FILE': str(policy)})


def test_fixed_proxy_fails_closed_when_policy_missing_or_empty_path(tmp_path):
    for path in ('', str(tmp_path / 'missing')):
        with pytest.raises(SystemExit):
            EP.context_from_env({'EGRESS_FIXED_ALLOW_FILE': path})

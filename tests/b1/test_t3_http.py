"""T-3: 記憶コネクタの HTTP の部分（8080 と 8081）。starlette と httpx が無い環境では、このファイルだけを飛ばす。"""
import hashlib

import pytest

pytest.importorskip("starlette.testclient")
pytest.importorskip("httpx")

from axcommon import creds
from axcommon.redact import Redactor
from tests.fakes.b1.core_memconn_imports import McpClient
from memconn import app as memconn_app
from tests.fakes.b1.fake_mcp import FakeMcp
from tests.b1.test_t3_memory import Emit, rec

# --- HTTP（starlette がある環境）---------------------------------------------------------------------
tc = pytest.importorskip("starlette.testclient")
pytest.importorskip("httpx")


def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


@pytest.fixture
def http_parts(tmp_path):
    mcp = FakeMcp()
    tokens = creds.TokenTable({"sl-demo": _sha("tok-demo")})
    env = {"AX_HOST": "hub-host", "AX_MEMORY_OUTBOX": str(tmp_path / "ob"), "AX_EVENT_OUTBOX": str(tmp_path / "eb")}
    core, work, svc = memconn_app.build(env, mcp=McpClient("http://memory.invalid/mcp/", transport=mcp), redactor=Redactor(), sink=Emit(), tokens=tokens,
                                        run_heartbeat_loop=False)
    return core, work, svc, mcp


def test_http_remember_core_port(http_parts):
    core, _w, _s, mcp = http_parts
    with tc.TestClient(core) as c:
        r = c.post("/v1/remember", json=rec())
        assert r.status_code == 200 and r.json()["status"] == "saved"
        assert c.post("/v1/remember", json=rec(project=None)).status_code == 422
        assert c.post("/v1/remember", content=b"nope").status_code == 400
        assert c.get("/healthz").json()["component"] == "memory-connector"
        assert c.get("/v1/memories/mem-0001").status_code == 200
        assert c.post("/v1/search", json={"query": "ボス"}).status_code == 200


def test_http_work_port_needs_token_and_runs_the_same_checks(http_parts):
    _c, work, _s, mcp = http_parts
    with tc.TestClient(work) as w:
        assert w.post("/v1/remember", json=rec()).status_code == 401
        assert w.post("/v1/remember", json=rec(), headers={"Authorization": "Bearer bad"}).status_code == 401
        h = {"Authorization": "Bearer tok-demo"}
        assert w.post("/v1/remember", json=rec(project=None), headers=h).status_code == 422
        secret = w.post("/v1/remember", json=rec(content="tel 090-1234-5678"), headers=h)
        assert secret.status_code == 422 and secret.json()["error"]["code"] == "secret_detected"
        assert w.post("/v1/remember", json=rec(), headers=h).status_code == 200
        assert {r.path for r in work.routes} == {"/healthz", "/v1/remember", "/v1/search", "/v1/memories/{memory_id}", "/v1/memories"}
        assert w.get("/healthz").json()["plugin_tokens"] is True


def test_configured_scope_reaches_memory_gateway_over_http(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent('''\
        from starlette.testclient import TestClient
        from axcommon.redact import Redactor
        from tests.fakes.b1.core_memconn_imports import McpClient
        from tests.fakes.b1.fake_mcp import FakeMcp
        from tests.b1.test_t3_memory import Emit, rec
        from memconn.app import build
        import os

        mcp = FakeMcp()
        core, _, _ = build({"AX_MEMORY_OUTBOX": os.environ["TEST_OUTBOX"]},
                           mcp=McpClient("http://memory.invalid/mcp/", transport=mcp),
                           redactor=Redactor(), sink=Emit(), run_heartbeat_loop=False)
        with TestClient(core) as client:
            assert client.post("/v1/remember", json=rec()).status_code == 200
            assert client.post("/v1/search", json={"query": "example"}).status_code == 200
        assert mcp.calls[0][1]["user"] == "configured-user"
        assert mcp.calls[1][1]["memory"]["scope"]["user"] == "configured-user"
        assert mcp.calls[2][1]["user"] == "configured-user"
    ''')
    env = {**os.environ, "AX_MEMCONN_USER": "configured-user", "TEST_OUTBOX": str(tmp_path / "outbox")}
    subprocess.run([sys.executable, "-c", script], env=env, check=True)


def test_scoped_memory_credential_cannot_change_fake_store(tmp_path):
    import copy

    fake = FakeMcp()
    table = creds.TokenTable({"legacy": _sha("write-token"), "memory.read:memory-digest": _sha("read-token"),
                              "memory.write:writer": _sha("write-only-token")})
    _, app, _ = memconn_app.build({"AX_MEMORY_OUTBOX": str(tmp_path / "outbox")},
        mcp=McpClient("http://memory.invalid/mcp/", transport=fake), redactor=Redactor(),
        sink=Emit(), tokens=table, run_heartbeat_loop=False)
    read = {"Authorization": "Bearer read-token"}
    write = {"Authorization": "Bearer write-token"}
    with tc.TestClient(app) as client:
        assert client.post('/v1/remember', json=rec(), headers=write).status_code == 200
        before = copy.deepcopy(fake.memories)
        assert client.get('/v1/memories', headers=read).status_code == 200
        assert client.get('/v1/memories/mem-0001', headers=read).status_code == 200
        assert client.get('/v1/search', params={'query': 'example'}, headers=read).status_code == 200
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            assert client.request(method, '/v1/remember', json=rec(), headers=read).status_code == 403
        assert client.post('/v1/search', json={'query': 'example'}, headers=read).status_code == 403
        writer = {"Authorization": "Bearer write-only-token"}
        assert client.get('/v1/memories', headers=writer).status_code == 403
        assert client.post('/v1/search', json={'query': 'example'}, headers=writer).status_code == 403
        assert client.get('/v1/memories').status_code == 401
        assert client.get('/v1/memories', headers={'Authorization': 'Bearer invalid'}).status_code == 401
        assert fake.memories == before
        assert client.post('/v1/remember', json=rec(content='write only'), headers=writer).status_code == 200


def test_timer_job_uses_scoped_credential_over_http(tmp_path, monkeypatch):
    import asyncio
    import copy
    from pathlib import Path
    import yaml
    from axcommon.client import ApiError
    from axcommon import schemas
    from runtime.subloop.store import Store
    from runtime.timer_runner.__main__ import job_client
    from runtime.timer_runner.runner import TimerRunner

    root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((root / 'plugins/timer/memory-digest/manifest.yaml').read_text())
    assert manifest['memory_capabilities'] == ['memory.read']
    assert not schemas.errors('manifest', manifest)
    secrets = tmp_path / 'secrets'
    secrets.mkdir()
    (secrets / 'plugin-token-memory-digest').write_text('legacy-token')
    (secrets / 'plugin-token-memory-read--memory-digest').write_text('scoped-token')
    monkeypatch.setenv('AX_SECRETS_PATH', str(secrets))
    table_file = tmp_path / 'tokens'
    table_file.write_text('memory-digest ' + _sha('legacy-token') + '\n' +
                          'memory.read:memory-digest ' + _sha('scoped-token') + '\n')
    table = creds.TokenTable.from_file(table_file)
    fake = FakeMcp()
    _, app, _ = memconn_app.build({'AX_MEMORY_OUTBOX': str(tmp_path / 'outbox')},
        mcp=McpClient('http://memory.invalid/mcp/', transport=fake), redactor=Redactor(),
        sink=Emit(), tokens=table, run_heartbeat_loop=False)
    store = Store(tmp_path / 'timer.sqlite')
    result = []
    with tc.TestClient(app) as client:
        assert client.post('/v1/remember', json=rec(), headers={'Authorization': 'Bearer legacy-token'}).status_code == 200
        before = copy.deepcopy(fake.memories)
        def transport(method, url, headers, body, timeout):
            response = client.request(method, url, headers=headers, content=body)
            return response.status_code, dict(response.headers), response.content
        def factory(pid, name):
            return job_client(pid, name, {'memory-digest': manifest}, environ={}, transport=transport)
        async def handler(ctx):
            c = ctx.clients['timeline']
            assert c.get('/v1/memories/mem-0001')
            with pytest.raises(ApiError) as error:
                c.post('/v1/remember', body=rec())
            result.append(error.value.status)
        runner = TimerRunner('timer-runner', store, Emit(), {'memory-digest': handler},
                             {'memory-digest': manifest}, factory)
        try:
            asyncio.run(runner.run_job('memory-digest', '2030-01-01T00:00:00Z'))
        finally:
            store.close()
        assert result == [403]
        assert fake.memories == before
        legacy = job_client('memory-digest', 'memory-connector', {'memory-digest': {}},
                            environ={}, transport=transport)
        assert legacy.post('/v1/remember', body=rec(content='another record'))['status'] == 'saved'
        missing = job_client('memory-digest', 'memory-connector',
                             {'memory-digest': {'memory_capabilities': ['unsupported']}},
                             environ={}, transport=transport)
        with pytest.raises(ApiError) as error:
            missing.post('/v1/remember', body=rec())
        assert error.value.status == 401

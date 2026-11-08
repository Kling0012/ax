"""HQ boundary behavior: sample writes and public restrictions, no network."""
from __future__ import annotations

import ipaddress as _fixture_ipaddress
_NET_FIXTURE_3258 = str(_fixture_ipaddress.ip_network("100.64.0.0/10")[3258])

from core.hq.app import ReadState, create_app
from core.hq.auth import AccessVerifier, Authenticator
from core.hq.secrets import SecretSlots
from core.hq.settings import Settings
from tests.fakes.b10b.runtime import JWKS, call, signed


class Sink:
    def emit(self, *args, **kwargs):
        pass


def app(tmp_path, *, sample=False, ui_dir=None):
    slots_dir = tmp_path / "slots"
    slots_dir.mkdir(mode=0o770)
    users = tmp_path / "users"
    users.write_text("test-user-3@example.com\n")
    aud = tmp_path / "aud"
    aud.write_text("test-aud")
    settings = Settings(ui_dir=ui_dir or tmp_path / "no-ui", public_proxy_ip="192.0.2.1", issuer="https://access.ci.invalid", jwks_url="https://keys.ci.invalid/certs",
                        aud_file=aud, users_file=users, slots_dir=slots_dir, email_slot="hq-access-email", sample=sample,
                        origins=frozenset({"https://hq.ci.invalid"}), slots={"local-key": {"file": "local-key", "operations": ["write"], "max_length": 32, "pattern": r"[A-Za-z]+", "target": {"local": True}}})
    verifier = AccessVerifier(settings.issuer, lambda: "test-aud", lambda: JWKS, clock=lambda: 1000)
    authentic = Authenticator(settings.public_proxy_ip, lambda: {"test-user-3@example.com"}, verifier)
    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir()
    return create_app(settings=settings, authenticator=authentic,
                      secret_slots=SecretSlots(slots_dir, settings.slots, emit=Sink().emit), run_heartbeat_loop=False, ext_dir=ext_dir)


def headers(*, public=False, origin=False):
    out = {"x-forwarded-for": "192.0.2.1" if public else "__NET_FIXTURE_3258__".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258)}
    if public:
        out["cf-access-jwt-assertion"] = signed()
    else:
        out["tailscale-user-login"] = "test-user-3@example.com"
    if origin:
        out["origin"] = "https://hq.ci.invalid"
    return out


def test_sample_rejects_write_but_retains_read_and_draft_is_client_owned(tmp_path):
    application = app(tmp_path, sample=True)
    response = call(application, "GET", "/v1/secrets", headers=headers())
    assert response.status_code == 200
    response = call(application, "POST", "/v1/secrets/local-key", headers=headers(origin=True), data={"value": "value"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "sample_read_only"
    assert not (tmp_path / "slots" / "local-key").exists()


def test_public_rejects_secret_and_direct_only_routes_before_data_parsing(tmp_path):
    application = app(tmp_path)
    response = call(application, "POST", "/v1/secrets/local-key", headers=headers(public=True, origin=True), data={"value": "value"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "secret_local_only"
    for path in ("/seats", "/v1/hq/seats/prepare", "/v1/hq/approval/enroll"):
        response = call(application, "GET", path, headers=headers(public=True))
        assert response.status_code == 404


def test_public_requires_origin_for_every_write(tmp_path):
    response = call(app(tmp_path), "POST", "/v1/secrets/local-key", headers=headers(public=True), data={"value": "value"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "secret_local_only"


def test_runtime_rejects_socket_directory_with_wrong_owner_mode_or_existing_socket(tmp_path):
    import os
    from core.hq.runtime import validate_socket_directory

    root = tmp_path / "hq"
    root.mkdir(mode=0o700)
    socket_path = root / "hq.sock"
    validate_socket_directory(socket_path, os.getuid())
    root.chmod(0o755)
    try:
        validate_socket_directory(socket_path, os.getuid())
    except PermissionError as exc:
        assert str(exc) == "hq_socket_directory_unsafe"
    else:
        raise AssertionError("permissive directory was accepted")
    root.chmod(0o700)
    socket_path.touch()
    try:
        validate_socket_directory(socket_path, os.getuid())
    except FileExistsError as exc:
        assert str(exc) == "hq_socket_exists_archive_before_restart"
    else:
        raise AssertionError("existing socket path was accepted")


def test_read_state_distinguishes_failure_from_sample_and_tracks_success():
    read = ReadState(clock=lambda: "2030-01-01T00:00:00.000+00:00")
    assert call_result(read, lambda: {"state": "ok"}) == {"status": "ok", "sample": False, "data": {"state": "ok"}, "last_success_at": "2030-01-01T00:00:00.000+00:00"}
    assert call_result(read, lambda: (_ for _ in ()).throw(RuntimeError()))["status"] == "unavailable"
    assert call_result(read, lambda: {"state": "fixture"}, sample=True)["sample"] is True
    assert read.last["resource"] == "2030-01-01T00:00:00.000+00:00"


def call_result(read, loader, sample=False):
    import asyncio
    return asyncio.run(read.fetch("resource", loader, sample=sample))


def test_pool_gateway_authentication_and_targeted_cancel(tmp_path):
    from core.hq.ext import b10pool

    application = app(tmp_path)
    b10pool.register(application)

    class Pool:
        def __init__(self):
            self.jobs = {"one": "running", "two": "running"}
        def get(self, path, **kw):
            return {"path": path, "jobs": dict(self.jobs)}
        def post(self, path, body):
            if path.endswith("/cancel"):
                self.jobs[path.split("/")[3]] = "cancelled"
            return {"path": path, "jobs": dict(self.jobs)}

    application.state.hq_pool = Pool()
    assert call(application, "GET", "/v1/pool/jobs", headers=headers()).status_code == 200
    assert call(application, "POST", "/v1/pool/jobs/one", headers=headers(), data={}).status_code == 403
    response = call(application, "POST", "/v1/pool/jobs/one", headers=headers(origin=True), data={})
    assert response.status_code == 200
    assert response.json()["jobs"] == {"one": "cancelled", "two": "running"}
    assert call(application, "POST", "/v1/pool/slots", headers=headers(origin=True), data={}).status_code == 400


def test_hq_pool_gateway_reaches_pool_and_resources(tmp_path, monkeypatch):
    from axcommon.creds import sha256_hex
    table = tmp_path / "pool-core-tokens"
    table.write_text("intake " + sha256_hex("test-intake") + "\n")
    monkeypatch.setenv("AX_CORE_TOKENS", str(table))
    import importlib
    from urllib.parse import urlsplit
    from starlette.testclient import TestClient
    from axcommon.client import ApiClient
    from core.hq.ext import b10pool

    class Resources:
        def get(self, path, **kwargs):
            return {path.rsplit("/", 1)[-1]: [{"model": "fake", "reason": "available"}]}

    module = importlib.import_module("plugins.resource.worker-pool.app")
    pool, _, _ = module.build(module.Config(db=str(tmp_path / "pool.sqlite")), resources=Resources(), run_heartbeat_loop=False)
    transport_client = TestClient(pool)
    def transport(method, url, headers, body, timeout):
        url = urlsplit(url)
        response = transport_client.request(method, url.path + (("?" + url.query) if url.query else ""), headers=headers, content=body)
        return response.status_code, dict(response.headers), response.content

    application = app(tmp_path)
    b10pool.register(application)
    application.state.hq_pool = ApiClient("http://pool", token="test-intake", transport=transport)
    job = {"role": "research", "prompt": "fake", "sensitivity": "public"}
    invalid = call(application, "POST", "/v1/pool/jobs", headers=headers(origin=True),
                   data={**job, "id": "forged", "state": "running"})
    assert invalid.status_code == 422
    assert not pool.state.service.store.list()
    first = call(application, "POST", "/v1/pool/jobs", headers=headers(origin=True), data=job).json()["job_id"]
    second = call(application, "POST", "/v1/pool/jobs", headers=headers(origin=True), data=job).json()["job_id"]
    assert call(application, "GET", "/v1/pool/jobs/" + first, headers=headers()).json()["id"] == first
    assert call(application, "GET", "/v1/pool/ledger", headers=headers()).json()["ledger"]
    assert call(application, "GET", "/v1/pool/selections", headers=headers()).json()["selections"]
    assert call(application, "POST", "/v1/pool/jobs/" + first, headers=headers(origin=True), data={}).status_code == 200
    assert call(application, "GET", "/v1/pool/jobs/" + second, headers=headers()).json()["state"] == "queued"
    mcp_list = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    names = call(application, "POST", "/v1/pool/mcp", headers=headers(origin=True), data=mcp_list).json()["result"]["tools"]
    assert {tool["name"] for tool in names} >= {"pool_submit", "pool_result", "pool_cancel", "pool_slots", "pool_ledger"}
    invoke = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "pool_result", "arguments": {"job_id": second}}}
    assert call(application, "POST", "/v1/pool/mcp", headers=headers(origin=True), data=invoke).json()["result"]["isError"] is False
    invoke["params"] = {"name": "pool_cancel", "arguments": {"job_id": "../other"}}
    assert call(application, "POST", "/v1/pool/mcp", headers=headers(origin=True), data=invoke).json()["result"]["isError"] is True
    assert call(application, "GET", "/v1/pool/jobs/" + second, headers=headers()).json()["state"] == "queued"


def test_t52_hq_screen_requires_auth_and_serves_assets(tmp_path):
    from core.hq.ext import b10ui
    application = app(tmp_path)
    b10ui.register(application)
    assert call(application, "GET", "/b10/", headers={}).status_code == 403
    page = call(application, "GET", "/b10/", headers=headers())
    assert page.status_code == 200 and "管理下の予定" in page.text
    assert call(application, "GET", "/b10/app.js", headers=headers()).status_code == 200
    assert call(application, "GET", "/b10/private", headers=headers()).status_code == 404


def test_tailnet_identity_and_origin_through_http_boundary(tmp_path):
    from core.hq.ext import b10pool

    application = app(tmp_path)
    b10pool.register(application)

    class Pool:
        def __init__(self):
            self.calls = []

        def post(self, path, body):
            self.calls.append((path, body))
            return {"accepted": True}

    pool = Pool()
    application.state.hq_pool = pool
    endpoint = "/v1/pool/jobs/example"
    good = headers(origin=True)
    assert call(application, "POST", endpoint, headers=good, data={}).status_code == 200
    assert len(pool.calls) == 1

    unauthorized = {**good, "tailscale-user-login": "unlisted@example.invalid"}
    missing = {key: value for key, value in good.items() if key != "tailscale-user-login"}
    forged_origin = {**good, "origin": "https://attacker.invalid"}
    cross_site = {**good, "sec-fetch-site": "cross-site"}
    for rejected in (unauthorized, missing, forged_origin, cross_site):
        assert call(application, "POST", endpoint, headers=rejected, data={}).status_code == 403
    assert len(pool.calls) == 1


def test_pool_public_routes_reject_untrusted_requests_and_cancel_one_job(tmp_path):
    from core.hq.ext import b10pool

    application = app(tmp_path)
    b10pool.register(application)

    class Pool:
        def __init__(self):
            self.jobs = {"one": "running", "two": "running"}
            self.calls = []

        def get(self, path, **kwargs):
            self.calls.append(("GET", path))
            return dict(self.jobs)

        def post(self, path, body):
            self.calls.append(("POST", path))
            if path.endswith("/cancel"):
                self.jobs[path.split("/")[3]] = "cancelled"
            return dict(self.jobs)

    pool = application.state.hq_pool = Pool()
    cancel = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pool_cancel", "arguments": {"job_id": "one"}}}
    paths = [("GET", "/v1/pool/jobs", None), ("POST", "/v1/pool/jobs/one", {}),
             ("POST", "/v1/pool/mcp", cancel)]
    bad_headers = [{}, {**headers(origin=True), "origin": "https://evil.invalid"},
                   {**headers(origin=True), "sec-fetch-site": "cross-site"},
                   {**headers(origin=True), "sec-fetch-site": "Cross-Site"}]
    for method, path, data in paths:
        for bad in bad_headers:
            assert call(application, method, path, headers=bad, data=data).status_code == 403
    assert pool.calls == []
    assert call(application, "POST", "/v1/pool/jobs/one", headers=headers(origin=True), data={}).status_code == 200
    assert pool.jobs == {"one": "cancelled", "two": "running"}
    cancel["params"]["arguments"]["job_id"] = "two"
    assert call(application, "POST", "/v1/pool/mcp", headers=headers(origin=True), data=cancel).json()["result"]["isError"] is False
    assert pool.calls[-1] == ("POST", "/v1/jobs/two/cancel")


def test_hq_rejects_explicitly_denied_identity_on_other_routes(tmp_path):
    application = app(tmp_path)
    application.state.hq_auth = lambda request: {"allowed": False}
    assert call(application, "GET", "/v1/secrets", headers=headers(origin=True)).status_code == 403


def test_pool_read_allows_missing_origin_but_writes_and_cross_site_do_not(tmp_path):
    from core.hq.ext import b10pool

    application = app(tmp_path)
    b10pool.register(application)

    class Pool:
        def __init__(self):
            self.calls = []

        def get(self, path, **kwargs):
            self.calls.append(("GET", path))
            return {"jobs": []}

        def post(self, path, body):
            self.calls.append(("POST", path))
            return {}

    pool = application.state.hq_pool = Pool()
    assert call(application, "GET", "/v1/pool/jobs", headers=headers()).status_code == 200
    assert call(application, "HEAD", "/v1/pool/jobs", headers=headers()).status_code == 200
    before = list(pool.calls)
    assert call(application, "GET", "/v1/pool/jobs", headers={**headers(), "sec-fetch-site": "cross-site"}).status_code == 403
    assert call(application, "POST", "/v1/pool/jobs/one", headers=headers(), data={}).status_code == 403
    assert pool.calls == before


def test_hq_pool_real_http_credentials_and_missing_secret(tmp_path, monkeypatch):
    import contextlib
    import importlib
    import socket
    import _socket
    import threading
    import httpx

    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            raise RuntimeError("external network forbidden")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    import uvicorn
    from axcommon.creds import sha256_hex
    from core.hq.ext import b10pool

    secrets = tmp_path / "service-secrets"
    secrets.mkdir()
    (secrets / "core-token-hq").write_text("fictional-hq-token")
    table = tmp_path / "pool-tokens"
    table.write_text("hq " + sha256_hex("fictional-hq-token") + "\nintake " + sha256_hex("fictional-intake-token") + "\n")
    plugin_table = tmp_path / "plugin-tokens"
    plugin_table.write_text("change " + sha256_hex("fictional-change-token") + "\n")
    monkeypatch.setenv("AX_SECRETS_PATH", str(secrets))
    monkeypatch.setenv("AX_CORE_TOKENS", str(table))
    monkeypatch.setenv("AX_PLUGIN_TOKENS", str(plugin_table))
    module = importlib.import_module("plugins.resource.worker-pool.app")
    pool, work, _ = module.build(module.Config(db=str(tmp_path / "real-pool.sqlite")), run_heartbeat_loop=False)

    @contextlib.contextmanager
    def serve(application):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        server = uvicorn.Server(uvicorn.Config(application, log_level="critical", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        import time
        for _ in range(500):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("server failed to start")
            time.sleep(.01)
        assert server.started
        try:
            yield "http://127.0.0.1:" + str(sock.getsockname()[1])
        finally:
            server.should_exit = True
            thread.join(5)
            sock.close()
            assert not thread.is_alive()

    with serve(pool) as pool_url, serve(work) as work_url:
        monkeypatch.setenv("AX_WORKER_POOL_URL", pool_url)
        application = app(tmp_path)
        b10pool.register(application)
        with serve(application) as hq_url, httpx.Client() as client:
            assert client.get(pool_url + "/v1/jobs").status_code in (401, 403)
            assert client.get(pool_url + "/v1/jobs", headers={"Authorization": "Bearer fictional-change-token"}).status_code in (401, 403)
            assert client.get(work_url + "/v1/jobs", headers={"Authorization": "Bearer fictional-hq-token"}).status_code in (401, 403)
            job = {"role": "research", "prompt": "fictional", "sensitivity": "public"}
            response = client.post(hq_url + "/v1/pool/jobs", headers=headers(origin=True), json=job)
            assert response.status_code == 200
            job_id = response.json()["job_id"]
            assert client.get(hq_url + "/v1/pool/jobs/" + job_id, headers=headers()).status_code == 200
            cancelled = client.post(hq_url + "/v1/pool/jobs/" + job_id, headers=headers(origin=True), json={})
            assert cancelled.status_code == 200
            assert "fictional-hq-token" not in cancelled.text
        monkeypatch.setenv("AX_SECRETS_PATH", str(tmp_path / "missing"))
        second = tmp_path / "missing-hq"
        second.mkdir()
        missing_app = app(second)
        b10pool.register(missing_app)
        with serve(missing_app) as hq_url, httpx.Client() as client:
            response = client.get(hq_url + "/v1/pool/jobs", headers=headers())
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "pool_credential_missing"
            rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pool_list", "arguments": {}}}
            result = client.post(hq_url + "/v1/pool/mcp", headers=headers(origin=True), json=rpc).json()
            assert result["result"]["content"][0]["text"] == "pool_credential_missing"
            assert client.get(hq_url + "/v1/secrets", headers=headers()).status_code == 200

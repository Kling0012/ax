from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from datetime import datetime, timedelta, timezone

from core.gate.config import Settings
from core.gate.service import Gate
from core.gate.contracts import Reply
from tests.fakes.b12b1.gate.parts import FakeRegistry, FakeRelay


def run(coro):
    return asyncio.run(coro)


def make_gate(tmp_path, *, tier="read", source_ip=str(ipaddress.ip_network("100.64.0.0/10")[3010]), read_per_s=10, output=256 * 1024):
    state = tmp_path / "gate"
    state.mkdir()
    seat_dir = tmp_path / "seat"
    seat_dir.mkdir()
    allowed = tmp_path / "allowed-users"
    allowed.write_text("test-user-6@example.com\n")
    token = "token-one"
    core = tmp_path / "core-token"
    core.write_text("a" * 64)
    peer = {"device": "device.example.invalid", "device_id": "stable-one", "os": "linux", "user_login": "test-user-6@example.com", "online": True}
    (state / "peers.json").write_text(json.dumps({source_ip: peer, str(ipaddress.ip_network("100.64.0.0/10")[3099]): {**peer, "device_id": "stable-two"}}))
    revoked = state / "revoked.d"
    revoked.mkdir()
    now = datetime.now(timezone.utc)
    row = {"seat_id": "seat-one", "label": "test", "via": "tailnet", "device": peer["device"], "device_id": peer["device_id"],
           "token_digest": hashlib.sha256(token.encode()).hexdigest(), "tier": tier, "state": "active",
           "created_at": (now - timedelta(minutes=1)).isoformat(), "created_by": "approval-test", "expires_at": (now + timedelta(days=1)).isoformat()}
    seats = seat_dir / "seats.yaml"
    import yaml
    seats.write_text(yaml.safe_dump({"seats": [row]}))
    settings = Settings(host="ax.tailnet.test", bind_addr=str(ipaddress.ip_network("100.64.0.0/10")[3001]), state_dir=state, seats_file=seats,
                        core_token_file=core, allowed_users_file=allowed, config_dir=tmp_path, port=8909,
                        gate_read_per_s=read_per_s, ax_output_max_kib=max(1, output // 1024))
    catalog = {"reads": [{"op": "status", "tool": "ax_status", "path": "/v1/read/status", "tier": "read"}],
               "destinations": [{"name": "work.request", "title": "work", "mcp_tool": "ax_work", "tier": "operate", "via": ["tailnet"], "allowed_from": ["commander"]}]}
    registry = FakeRegistry({"work.request": catalog["destinations"][0]})
    relay = FakeRelay(catalog)
    return Gate(settings, transport=relay, registry=registry), relay, row, source_ip, token


def headers(token="token-one", *, host="ax.tailnet.test:8909", origin=None, extra=()):
    items = [("host", host), ("authorization", "Bearer " + token)]
    if origin is not None:
        items.append(("origin", origin))
    return [*items, *extra]


def test_t195_host_origin_peer_and_spoofed_headers(tmp_path):
    gate, relay, row, ip, token = make_gate(tmp_path)
    assert run(gate.dispatch("GET", "/v1/read/status", headers(token, host="wrong:8909"), ip)).status == 421
    assert run(gate.dispatch("GET", "/v1/read/status", headers(token, origin="https://browser.test"), ip)).status == 403
    reply = run(gate.dispatch("GET", "/v1/read/status", headers(token, extra=[("x-forwarded-for", str(ipaddress.ip_network("100.64.0.0/10")[3099])), ("tailscale-user-login", "forged")]), ip))
    assert reply.status == 200
    assert relay.calls[-1].evidence.device_id == "stable-one"
    assert run(gate.dispatch("GET", "/v1/read/status", headers(token), str(ipaddress.ip_network("100.64.0.0/10")[3077]))).data["error"]["code"] == "peer_unknown"
    assert run(gate.dispatch("GET", "/v1/read/status", headers(token), str(ipaddress.ip_network("100.64.0.0/10")[3001]))).data["error"]["code"] == "peer_unknown"
    (tmp_path / "allowed-users").write_text("test-user-7@example.com\n")
    assert run(gate.dispatch("GET", "/v1/read/status", headers(token), ip)).data["error"]["code"] == "peer_unknown"


def test_t196_credential_binds_to_device_revoke_expiry_and_secret_redaction(tmp_path):
    gate, relay, row, ip, token = make_gate(tmp_path)
    assert run(gate.dispatch("GET", "/v1/read/status", headers(token), str(ipaddress.ip_network("100.64.0.0/10")[3099]))).data["error"]["code"] == "unauthorized"
    (tmp_path / "gate" / "revoked.d" / "seat-one").touch()
    assert run(gate.dispatch("GET", "/v1/read/status", headers(token), ip)).data["error"]["code"] == "seat_revoked"
    expired_root = tmp_path / "expired"
    expired_root.mkdir()
    expired, _, expired_row, expired_ip, expired_token = make_gate(expired_root)
    expired_row["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    import yaml
    (expired_root / "seat" / "seats.yaml").write_text(yaml.safe_dump({"seats": [expired_row]}))
    assert run(expired.dispatch("GET", "/v1/read/status", headers(expired_token), expired_ip)).data["error"]["code"] == "seat_expired"
    redaction_root = tmp_path / "redaction"
    redaction_root.mkdir()
    redaction, redaction_relay, _, redaction_ip, redaction_token = make_gate(redaction_root)
    redaction_relay.replies[("GET", "/v1/read/status")] = Reply(200, {"token": redaction_token, "token_digest": "not-returned", "ok": True})
    reply = run(redaction.dispatch("GET", "/v1/read/status", headers(redaction_token), redaction_ip))
    assert reply.data == {"ok": True}
    assert redaction_token not in repr(redaction_relay.calls[-1])


def test_t197_limits_body_output_and_upstream_failure_are_local(tmp_path):
    gate, relay, row, ip, token = make_gate(tmp_path, read_per_s=1, output=1024)
    too_large = run(gate.dispatch("POST", "/v1/instructions", headers(token), ip, b"x" * (65 * 1024)))
    assert too_large.status == 413
    first = run(gate.dispatch("GET", "/v1/read/status", headers(token), ip))
    second = run(gate.dispatch("GET", "/v1/read/status", headers(token), ip))
    assert first.status == 200 and second.status == 429 and second.headers["Retry-After"] == "1"
    gate.limits.calls.clear()
    relay.replies[("GET", "/v1/read/status")] = Reply(200, {"data": ["x" * 2048]})
    bounded = run(gate.dispatch("GET", "/v1/read/status", headers(token), ip))
    assert bounded.data["truncated"] is True and bounded.data["next"]["path"] == "/v1/read/status"
    gate.limits.calls.clear()
    async def unavailable(request, timeout):
        raise OSError("unavailable")
    gate.transport = unavailable
    reply = run(gate.dispatch("GET", "/v1/read/status", headers(token), ip))
    assert reply.status == 200 and reply.data["intake"]["reason"] == "upstream_unavailable"
    assert run(gate.dispatch("GET", "/healthz", [("host", "ax.tailnet.test:8909")], ip)).data == {"ok": True}


def test_s615_mcp_hides_tiered_tools_and_maps_read_requests(tmp_path):
    gate, relay, row, ip, token = make_gate(tmp_path, tier="read")
    listed = run(gate.dispatch("POST", "/mcp", headers(token), ip, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()))
    tools = listed.data["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["ax_status"]
    assert tools[0]["annotations"]["readOnlyHint"] is True
    called = run(gate.dispatch("POST", "/mcp", headers(token), ip, json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ax_status", "arguments": {}}}).encode()))
    assert called.data["result"]["isError"] is False
    assert relay.calls[-1].path == "/v1/read/status"
    denied = run(gate.dispatch("POST", "/mcp", headers(token), ip, json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ax_work", "arguments": {"request_id": "r1"}}}).encode()))
    assert denied.data["result"]["isError"] is True


def test_healthz_has_no_identity_or_component_details(tmp_path):
    from core.gate.app import build_app
    gate, _, _, ip, _ = make_gate(tmp_path)
    app = build_app(gate)
    sent = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    run(app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "/healthz", "raw_path": b"/healthz", "root_path": "", "query_string": b"", "server": ("fake", 8909),
             "client": (ip, 1), "headers": [(b"host", b"ax.tailnet.test:8909")]}, receive, send))
    assert next(item["status"] for item in sent if item["type"] == "http.response.start") == 200
    assert b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body") == b'{"ok":true}'


def test_mcp_work_preserves_project_in_instruction_payload(tmp_path):
    gate, relay, _, ip, token = make_gate(tmp_path, tier="operate")
    relay.catalog["destinations"][0]["schema"] = "schemas/dest/work.request.json"
    arguments = {"request_id": "r1", "project": "demo", "title": "Research", "role": "research"}
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "ax_work", "arguments": arguments}}
    response = run(gate.dispatch("POST", "/mcp", headers(token), ip, json.dumps(request).encode()))
    assert response.data["result"]["isError"] is False
    assert relay.calls[-1].path == "/v1/instructions"
    assert relay.calls[-1].body["project"] == "demo"
    assert relay.calls[-1].body["payload"] == {"project": "demo", "title": "Research", "role": "research"}

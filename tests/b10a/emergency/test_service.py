"""Service authentication and the HQ -> intake emergency destination boundary."""
import json
from types import SimpleNamespace

import pytest

from axcommon import creds
from core.intake.app import build_app
from core.intake.dest.b10a import register
from core.intake.dest.b10a.handler import emergency_handler
from core.intake.dest.b10a.service import create_emergency_app
from core.intake.vias import Ident
from tests.fakes.b10a.asgi import call

HQ = Ident("hq", "hq", "user", "irreversible")
LOCAL = Ident("local", "commander", "local-example", "operate")


def service(emergency, entries=None):
    table = creds.TokenTable(entries=entries if entries is not None else {
        "intake": creds.sha256_hex("test-intake"), "observer": creds.sha256_hex("test-observer")})
    return create_emergency_app(emergency.runner, core_tokens=table, run_heartbeat_loop=False)


def test_service_accepts_only_intake_credential(emergency):
    app = service(emergency)
    for token in (None, "test-observer", "unknown"):
        headers = {"Authorization": "Bearer " + token} if token else {}
        status, _ = call(app, "POST", "/v1/emergency", body={"scope": "all"}, headers=headers)
        assert status == 401
    assert emergency.world.calls == []
    status, result = call(app, "POST", "/v1/emergency", body={"scope": "all", "request_id": "http"},
                          headers={"Authorization": "Bearer test-intake"})
    assert status == 200 and result["scope"] == "all"
    assert call(app, "GET", "/v1/emergency")[0] == 405
    assert call(app, "GET", "/healthz")[0] == 200


def test_missing_auth_table_rejects_locally(emergency):
    status, result = call(service(emergency, {}), "POST", "/v1/emergency", body={"scope": "all"})
    assert status == 503 and result["error"]["code"] == "auth_unavailable"
    assert emergency.world.calls == []


@pytest.mark.parametrize("body", [None, [], {"scope": 123}, {"scope": "../x"}, {"scope": "all", "issued_by": "user"},
                                      {"scope": "all", "request_id": 123}, {"scope": "all", "request_id": ""},
                                      {"scope": "all", "request_id": "x" * 201}])
def test_service_rejects_invalid_shape_before_effects(emergency, body):
    status, _ = call(service(emergency), "POST", "/v1/emergency", body=body,
                     headers={"Authorization": "Bearer test-intake"})
    assert status in (400, 422)
    assert emergency.world.calls == []


def test_service_unknown_scope_is_404(emergency):
    status, result = call(service(emergency), "POST", "/v1/emergency", body={"scope": "absent"},
                          headers={"Authorization": "Bearer test-intake"})
    assert status == 404 and result["error"]["code"] == "unknown_scope"


def make_intake(emergency, tmp_path, monkeypatch, repo_root):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "intake-destinations.yaml").write_text(
        (repo_root / "config/intake-destinations.d/b10a.yaml").read_text())
    monkeypatch.setenv("AX_CORE_TOKEN_FILE", str(tmp_path / "intake-token"))
    (tmp_path / "intake-token").write_text("test-intake")
    emergency_app = service(emergency)

    def transport(method, path, q, body, headers):
        status, out = call(emergency_app, method, path, body=body, headers=headers)
        return status, {}, json.dumps(out).encode()

    emergency.world.extra["emergency"] = transport
    app = build_app(config_dir=cfg, transport=emergency.world, sink=emergency.sink,
                    dest_dir=tmp_path / "no-other-destinations", ext_dir=tmp_path / "no-extensions", run_heartbeat_loop=False)
    register(app)

    class InlineExecutor:
        def submit(self, fn):
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(fn).result(timeout=10)

    app.state.intake.executor = InlineExecutor()
    return app


def as_ident(app, ident):
    async def wrap(scope, receive, send):
        scope["ax_ident"] = ident
        await app(scope, receive, send)
    return wrap


def test_hq_intake_client_routes_scope_and_id_with_approval(emergency, tmp_path, monkeypatch, repo_root):
    from core.hq.notify.intake import IntakeClient

    app = make_intake(emergency, tmp_path, monkeypatch, repo_root)
    received = []

    def intake_transport(method, path, raw):
        received.append(json.loads(raw))
        status, out = call(as_ident(app, HQ), method, path, body=json.loads(raw))
        return status, json.dumps(out).encode()

    client = IntakeClient(transport=intake_transport)
    status, result = client.submit("emergency", {"scope": "alpha"}, idempotency_key="hq-operation")
    assert status == 202 and result["reason"] == "approval_required"
    assert emergency.world.paths("emergency") == []
    status, result = client.submit("emergency", {"scope": "alpha"}, idempotency_key="hq-operation", confirm_irreversible=True)
    assert status == 202 and result["status"] == "pending"
    assert received[-1]["from"] == "hq" and received[-1]["payload"] == {"scope": "alpha"}
    assert received[-1]["confirm_irreversible"] is True
    assert emergency.world.paths("emergency", "POST") == ["/v1/emergency"]
    run = emergency.runner._previous("hq-operation")
    assert run["scope"] == "alpha" and run["request_id"] == "hq-operation"
    assert client.submit("emergency", {"scope": "alpha"}, idempotency_key="hq-operation", confirm_irreversible=True)[0] == 200
    assert len(emergency.world.paths("emergency", "POST")) == 1


def test_commander_cannot_forge_hq_or_route_emergency(emergency, tmp_path, monkeypatch, repo_root):
    app = make_intake(emergency, tmp_path, monkeypatch, repo_root)
    body = {"destination": "emergency", "payload": {"scope": "all"}, "idempotency_key": "fake", "confirm_irreversible": True}
    assert call(as_ident(app, LOCAL), "POST", "/v1/instructions", body=body)[0] == 403
    assert call(as_ident(app, LOCAL), "POST", "/v1/instructions", body={**body, "from": "hq"})[0] == 403
    assert emergency.world.paths("emergency") == []
    assert emergency_handler(app, {}, body, LOCAL)[0] == 403
    assert emergency_handler(app, {}, body, Ident("local", "hq", "user", "irreversible"))[0] == 403


def test_handler_lockdown_hook_and_missing_hook_are_explicit(emergency, tmp_path, monkeypatch, repo_root):
    app = make_intake(emergency, tmp_path, monkeypatch, repo_root)
    body = {"payload": {"scope": "commanders"}, "idempotency_key": "lockdown"}
    status, result = emergency_handler(app, {}, body, HQ)
    assert status == 200
    assert result["lockdown"] == {"done": False, "error": "lockdown_not_implemented"}
    seen = []
    app.state.intake.lockdown = lambda enabled, ident: seen.append((enabled, ident))
    status, result = emergency_handler(app, {}, {**body, "idempotency_key": "lockdown-2"}, HQ)
    assert status == 200 and result["lockdown"] == {"done": True}
    assert seen == [(True, HQ)]
    assert emergency.world.paths("worker-pool", "POST") == []


def test_registry_and_registration_contract(repo_root):
    from core.intake.registry import build_registry, parse_files

    docs, faults = parse_files([repo_root / "config/intake-destinations.d/b10a.yaml"])
    entries, build_faults = build_registry(docs)
    assert faults == build_faults == []
    dest = entries["emergency"]
    assert dest["allowed_from"] == ["hq"] and dest["via"] == ["hq"]
    assert dest["tier"] == "irreversible" and dest["ack"] == "async"
    assert dest["handler"] == "http://emergency:8080/v1/emergency"
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(dest_handlers={})))
    register(app)
    assert app.state.intake.dest_handlers["emergency"] is emergency_handler

"""Exercise control authentication through the intake and control APIs."""
from core.intake.vias import Ident
from tests.b5.helpers import client_for, instruction
from tests.b5.test_handlers import _control, _ledger

pytest_plugins = ["tests.b3.conftest"]


def test_control_relay(control_env, fake_transport, tmp_path):
    env = control_env
    secret = env["tokens"]["secrets"] / "core-token-intake"

    def forward(call):
        response = env["core"].request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content

    app = _control(fake_transport, tmp_path)
    fake_transport.add("POST", "/v1/commands", handler=forward)
    payload = {"target": "issue-automation", "op": "set_mode",
               "args": {"mode": "collect_only"}, "reason": "test"}

    def submit(key, ident=None):
        with client_for(app, ident) if ident else client_for(app) as client:
            return client.post("/v1/instructions", json=instruction("control.command", payload, idempotency_key=key))

    missing = submit("missing-token")
    assert missing.status_code == 401
    assert missing.json()["result"]["error"]["code"] == "unauthorized"
    assert not env["service"].store.living_commands()

    secret.write_text(env["tokens"]["core"]["intake"])
    response = submit("good")
    assert response.status_code == 200, response.json()
    assert env["service"].effective("issue-automation")["mode"] == "collect_only"
    assert response.json()["result"]["issued_by"] == "commander"
    before = len(env["service"].store.living_commands())

    secret.write_text("invalid-test-credential")
    denied = submit("bad-token")
    assert denied.status_code == 401
    assert denied.json()["result"]["error"]["code"] == "unauthorized"
    assert len(env["service"].store.living_commands()) == before

    secret.write_text(env["tokens"]["core"]["intake"])
    denied = submit("bad-tier", Ident("local", "commander", "test", "read"))
    assert denied.status_code == 403
    assert len(env["service"].store.living_commands()) == before
    assert len([call for call in fake_transport.calls if call.path == "/v1/commands"]) == 3


def test_other_relay_has_no_control_credential(b3_tokens, fake_transport, tmp_path):
    (b3_tokens["secrets"] / "core-token-intake").write_text(b3_tokens["core"]["intake"])
    app = _ledger(fake_transport, tmp_path)
    payload = {"todo_id": "td-9", "answer": "test", "quote": "test"}
    with client_for(app) as client:
        response = client.post("/v1/instructions", json=instruction("ledger.answer", payload, idempotency_key="ledger"))
    assert response.status_code == 200
    calls = [call for call in fake_transport.calls if call.path == "/v1/todos/td-9/answer"]
    assert len(calls) == 1 and "Authorization" not in calls[0].headers

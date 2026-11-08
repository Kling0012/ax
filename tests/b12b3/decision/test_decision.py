import argparse
import json
from types import SimpleNamespace

from core.intake.dest.b12b3 import decision
from host.lib.ax_ops import decision as cli


def test_handler_propagates_identity_and_rejects_forgery(monkeypatch):
    seen = []
    def transport(method, url, headers, data, timeout):
        seen.append((method, url, headers, json.loads(data)))
        return 201, {}, b'{"id":"created"}'
    monkeypatch.setattr(decision, "_token", lambda: "test-token")
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=transport)))
    ident = SimpleNamespace(via="local", from_="commander", principal="seat-1")
    body = {"payload": {"kind": "decision", "title": "Title", "body": "Body", "basis": "commander"}, "session": "session-1"}
    assert decision.handle(app, {}, body, ident) == (200, {"id": "created"})
    assert seen[0][2]["X-AX-Via"] == "local"
    assert seen[0][3]["recorded_by"] == {"principal": "seat-1", "session": "session-1"}
    assert decision.handle(app, {}, {"payload": {**body['payload'], "attested": True}}, ident)[0] == 422
    ident.via = "hq"
    assert decision.handle(app, {}, body, ident)[0] == 403
    assert len(seen) == 1


def test_failure_is_local_and_no_credential_leaks(monkeypatch):
    monkeypatch.setattr(decision, "_token", lambda: None)
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=None)))
    ident = SimpleNamespace(via="local", from_="commander", principal="seat")
    body = {"payload": {"kind": "decision", "title": "a", "body": "b", "basis": "design"}}
    assert decision.handle(app, {}, body, ident) == (503, {"error": {"code": "auth_unavailable"}})


def test_cli_decide_handoff_and_deterministic_id():
    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers(dest="command", required=True))
    glob = SimpleNamespace(request_id=None, session="s")
    args = parser.parse_args(["decide", "preference", "--title", "a", "--body", "b"])
    one = cli.build_request(args, glob)
    assert one == cli.build_request(args, glob)
    assert one[2]["payload"]["kind"] == "preference"
    args = parser.parse_args(["handoff", "next", "--project", "p"])
    assert cli.build_request(args, glob)[2]["payload"]["kind"] == "handoff"

from tests.b3.conftest import ledger_env, b3_tokens, TOK_INTAKE


def test_real_ledger_supersession_and_attestation(ledger_env, monkeypatch):
    monkeypatch.setattr(decision, "_token", lambda: TOK_INTAKE)
    def transport(method, url, headers, data, timeout):
        response = ledger_env["core"].request(method, "/v1/decisions", headers=headers, content=data)
        return response.status_code, dict(response.headers), response.content
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=transport)))
    ident = SimpleNamespace(via="hq", from_="hq", principal="user")
    payload = {"kind": "decision", "title": "first", "body": "body", "basis": "design"}
    status, first = decision.handle(app, {}, {"payload": payload}, ident)
    assert status == 200 and first["attested"] is True and first["recorded_by"]["principal"] == "user"
    ident = SimpleNamespace(via="ssh", from_="commander", principal="seat")
    status, second = decision.handle(app, {}, {"payload": {**payload, "supersedes": first["id"]}, "session": "s"}, ident)
    assert status == 200 and second["attested"] is False and second["recorded_by"] == {"principal": "seat", "session": "s"}
    assert ledger_env["core"].get('/v1/decisions/' + first['id']).json()['decision']['status'] == 'superseded'

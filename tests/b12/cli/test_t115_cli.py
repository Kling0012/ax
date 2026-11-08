"""T-115: 中継、CLI、再接続の呼び出し。通信はすべて偽。"""
from __future__ import annotations

import asyncio
from io import StringIO
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from starlette.applications import Starlette

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "host/lib"))
import ax_cli
from ax_cli.transport import EntryTransport, ResultUnknown, Unreachable, socket_path
from core.intake.audit import AccessLog
from core.intake.dest.b12.read_relay import ReadRelay, register
from core.intake.vias import Ident
from tests.fakes.b12.cli.entry import FakeConnection, FakeCore, FakeEntry, request

LOCAL = Ident("local", "commander", "local-example", "operate")


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError("照会が指示の状態へ触れた: " + name)


def relay_app(tmp_path, core=None):
    app = Starlette()
    core = core or FakeCore()
    audit = AccessLog(tmp_path / "access.log")
    app.state.intake = SimpleNamespace(transport=core, audit=audit, registry=Forbidden(),
                                      idem=Forbidden(), sink=Forbidden())
    register(app)
    return app, core, audit


def cli(argv, entry=None, env=None):
    stdout, stderr = StringIO(), StringIO()
    entry = entry or FakeEntry()
    code = ax_cli.main(argv, transport=entry, environ=env or {}, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue(), entry


def test_status_is_parallel_component_table_and_get_only(tmp_path):
    app, core, audit = relay_app(tmp_path)
    status, body = request(app, "/v1/read/status", ident=LOCAL)
    assert status == 200
    assert set(body) == {"control", "timeline"}
    assert set(body["control"]["data"]) == {"plugins", "commands"}
    assert set(body["timeline"]["data"]) == {"plugins", "runs", "controls"}
    assert len(core.calls) == 5
    assert all(method == "GET" and payload is None and "Authorization" not in headers and timeout == 5
               for method, _, headers, payload, timeout in core.calls)
    assert all(url.port == 8080 for _, url, _, _, _ in core.calls)
    assert next(url.query for _, url, _, _, _ in core.calls if url.path == "/v1/commands") == "state=active"
    assert audit.read_all()[0]["principal"] == "local-example"


@pytest.mark.parametrize("down", [{"control"}, {"timeline"}, {"control", "timeline"}])
def test_unavailable_isolated_and_still_http_200(tmp_path, down):
    app, core, _ = relay_app(tmp_path)
    core.down = down
    status, body = request(app, "/v1/read/status", ident=LOCAL)
    assert status == 200
    for component in ("control", "timeline"):
        if component in down:
            assert body[component] == {"ok": False, "error": "unavailable"}
        else:
            assert body[component]["ok"] is True


@pytest.mark.parametrize("op,allowed,query", [
    ("todos", {"state", "project", "subloop", "due"}, "state=open&project=a%2Bb&subloop=s&due=now&wait=1&token=bad"),
    ("events", {"after", "kind", "subloop", "project", "subject", "limit"}, "after=a&kind=k&subloop=s&project=p&subject=x&limit=10&wait=60&idempotency_key=x"),
])
def test_query_allowlist_never_forwards_wait_or_credentials(tmp_path, op, allowed, query):
    app, core, _ = relay_app(tmp_path)
    status, _ = request(app, "/v1/read/" + op, ident=LOCAL, query=query)
    assert status == 200
    assert len(core.calls) == 1
    assert set(parse_qs(core.calls[0][1].query)) == allowed
    if op == "todos":
        assert parse_qs(core.calls[0][1].query)["project"] == ["a+b"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "HEAD"])
def test_non_get_rejected_without_core_calls(tmp_path, method):
    app, core, audit = relay_app(tmp_path)
    assert request(app, "/v1/read/status", ident=LOCAL, method=method)[0] == 405
    assert not core.calls
    assert audit.read_all()[0]["status"] == 405


def test_unknown_route_and_no_authority(tmp_path):
    app, core, _ = relay_app(tmp_path)
    # Starlette の既定の404は text/plain。
    async def unknown():
        messages = []
        scope = {"type": "http", "method": "GET", "path": "/v1/read/unknown", "root_path": "", "headers": [], "query_string": b""}
        async def receive():
            return {"type": "http.request", "body": b""}
        async def send(msg):
            messages.append(msg)
        await app(scope, receive, send)
        return messages[0]["status"]
    assert asyncio.run(unknown()) == 404
    for ident in (None, Ident("hq", "hq", "user", "irreversible"), Ident("local", "commander", "x", "invalid")):
        assert request(app, "/v1/read/status", ident=ident)[0] == 403
    assert not core.calls


def test_read_tier_allowed_and_register_idempotent(tmp_path):
    app, core, audit = relay_app(tmp_path)
    register(app)
    read_seat = Ident("ssh", "commander", "seat-abc", "read")
    assert request(app, "/v1/read/events", ident=read_seat)[0] == 200
    assert len(core.calls) == len(audit.read_all()) == 1


def test_per_target_timeout_does_not_wait_for_other_components(monkeypatch):
    async def exercise():
        relay = ReadRelay(FakeCore(), timeout_s=1)
        original = relay.clients["control"].get
        def timeout(path, **kwargs):
            raise TimeoutError("fake timeout")
        monkeypatch.setattr(relay.clients["control"], "get", timeout)
        body = await relay.read("status", {})
        assert body["control"]["error"] == "unavailable"
        assert body["timeline"]["ok"]
        monkeypatch.setattr(relay.clients["control"], "get", original)
    asyncio.run(exercise())


def test_b5_app_register_does_not_emit_instruction_events(tmp_path):
    from core.intake.app import build_app
    config = tmp_path / "config"
    config.mkdir()
    (config / "intake-destinations.yaml").write_text("[]\n")
    events = []
    sink = SimpleNamespace(emit=lambda *a, **k: events.append((a, k)))
    app = build_app(config_dir=config, dest_dir=tmp_path / "no-dest", ext_dir=tmp_path / "no-ext",
                    sink=sink, transport=FakeCore(), run_heartbeat_loop=False)
    register(app)
    count = len(events)
    app.state.intake.idem = Forbidden()
    app.state.intake.registry = Forbidden()
    assert request(app, "/v1/read/status", ident=LOCAL)[0] == 200
    assert len(events) == count
    app.state.intake.executor.shutdown(wait=False)


def test_socket_discovery(tmp_path):
    home = tmp_path
    config = home / ".config/ax"
    config.mkdir(parents=True)
    default = str(home / ".local/state/ax/commander/commander.sock")
    assert socket_path({}, home) == default
    (config / "config.yaml").write_text('socket: "~/custom/commander.sock" # local\n')
    assert socket_path({}, home) == str(home / "custom/commander.sock")
    assert socket_path({"AX_SOCKET": "override.sock"}, home) == "override.sock"


@pytest.mark.parametrize("op", ["status", "todos", "events"])
def test_cli_reads_have_no_instruction_or_idempotency(op):
    code, out, _, entry = cli([op, "--json", "--request-id", "ignored"])
    assert code == 0
    assert entry.calls == [("GET", "/v1/read/" + op, None)]
    result = json.loads(out)
    assert result["op"] == op and result["request_id"] is None
    assert "命令ではない" in result["notice"]


def test_cli_filters_and_auto_json():
    code, out, _, entry = cli(["events", "--kind", "a b", "--subject", "x&y", "--limit", "2"])
    assert code == 0 and json.loads(out)["ok"]
    assert entry.calls[0][1] == "/v1/read/events?kind=a+b&subject=x%26y&limit=2"


def test_cli_text_partial_outage_still_success():
    entry = FakeEntry(response={"control": {"ok": False, "error": "unavailable"}, "timeline": {"ok": True, "data": ["other"]}})
    code, out, _, _ = cli(["--format", "text", "status"], entry)
    assert code == 0
    assert "control\tunavailable" in out and "timeline" in out and "other" in out and "[外部]" in out


def test_work_hash_is_stable_and_explicit_id_wins():
    argv = ["work", "--project", "p", "--title", "title", "--role", "impl"]
    first = cli(argv)[3].calls[0][2]
    second = cli(argv)[3].calls[0][2]
    assert first == second
    assert len(first["idempotency_key"]) == 64
    explicit = cli(argv + ["--request-id", "new-request", "--session", "session-a"])[3].calls[0][2]
    assert explicit["idempotency_key"] == "new-request"
    assert explicit["session"] == "session-a"
    assert explicit["destination"] == "work.request"


def test_control_ulid_and_lease():
    code, _, _, entry = cli(["control", "sl-demo", "set_mode", '{"mode":"draining"}', "--reason", "点検", "--lease", "15"])
    assert code == 0
    body = entry.calls[0][2]
    assert re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", body["idempotency_key"])
    assert body["destination"] == "control.command"
    assert body["payload"]["args"] == {"mode": "draining"}
    assert body["expires_at"].endswith("Z")
    assert "expires_at" not in body["payload"]
    assert not {"issued_by", "confirm_irreversible", "principal"} & body["payload"].keys()


def test_answer_requires_quote():
    entry = FakeEntry()
    assert cli(["answer", "todo-a", "answer"], entry)[0] == 1
    assert not entry.calls
    code, _, _, entry = cli(["answer", "todo-a", "answer", "--quote", "利用者の言葉"])
    assert code == 0 and entry.calls[0][2]["payload"]["quote"] == "利用者の言葉"


@pytest.mark.parametrize("error,expected", [
    ("socket_unreachable", 3), ("result_unknown", 5), ("tier_insufficient", 6),
    ("receptacle_not_allowed", 6), ("operation_not_allowed", 6), ("forged_authority", 6),
    ("seat_unknown", 7), ("seat_revoked", 7), ("seat_suspended", 7), ("seat_expired", 7),
    ("conflict", 8), ("preempted", 8), ("locked", 8), ("idempotency_conflict", 8), ("bad_request", 1),
])
def test_exit_codes(error, expected):
    code, out, _, _ = cli(["status"], FakeEntry(403, {"ok": False, "error": {"code": error}}))
    assert code == expected
    assert json.loads(out)["ok"] is False


def test_approval_and_unknown_result_output():
    code, out, _, _ = cli(["status"], FakeEntry(202, {"ok": True, "data": {"approval_id": "a"}}))
    assert code == 4 and json.loads(out)["data"]["approval_id"] == "a"
    entry = FakeEntry(error=ResultUnknown("fake"))
    code, out, _, _ = cli(["work", "--project", "p", "--title", "t", "--request-id", "same"], entry)
    assert code == 5 and json.loads(out)["hint"] == "ax show same"
    assert len(entry.calls) == 1


def test_unreachable_prints_socket_and_cause_on_one_line():
    code, out, err, entry = cli(["status"], FakeEntry(error=Unreachable("ConnectionRefusedError")))
    assert code == 3 and json.loads(out)["error"]["code"] == "socket_unreachable"
    assert err.count("\n") == 1 and entry.target in err and "ConnectionRefusedError" in err


def test_limit_preserves_valid_json(monkeypatch):
    monkeypatch.setattr(ax_cli, "default_value", lambda name, fallback: 16 if name == "ax_output_max_kib" else fallback)
    code, out, _, _ = cli(["status"], FakeEntry(response={"timeline": {"ok": True, "data": "あ" * 30000}}))
    assert code == 0 and len(out.encode()) <= 16 * 1024
    assert json.loads(out)["data"]["truncated"] is True
    assert json.loads(out)["next"]


def test_entry_reconnects_on_every_invocation(tmp_path):
    connections = []
    def factory(path):
        assert path == "fake.sock"
        conn = FakeConnection()
        connections.append(conn)
        return conn
    transport = EntryTransport({"AX_SOCKET": "fake.sock"}, tmp_path, connection_factory=factory)
    for _ in range(2):
        assert transport("GET", "/v1/read/status")[0] == 200
    assert len(connections) == 2
    assert all(conn.connects == conn.closed == 1 for conn in connections)


@pytest.mark.parametrize("stage,error", [("connect", Unreachable), ("response", ResultUnknown)])
def test_entry_distinguishes_unreachable_and_uncertain_write(tmp_path, stage, error):
    conn = FakeConnection(connect_error=OSError("fake") if stage == "connect" else None,
                          response_error=OSError("fake") if stage == "response" else None)
    transport = EntryTransport({"AX_SOCKET": "fake.sock"}, tmp_path, connection_factory=lambda path: conn)
    with pytest.raises(error):
        transport("POST", "/v1/instructions", {})
    assert conn.closed == 1


def test_gate_token_permission_and_no_secret_in_target(tmp_path):
    config = tmp_path / ".config/ax"
    config.mkdir(parents=True)
    token = config / "gate-token"
    token.write_text("fake-token")
    token.chmod(0o600)
    conn = FakeConnection()
    transport = EntryTransport({"AX_GATE_URL": "http://gate.invalid:8909"}, tmp_path, connection_factory=lambda *args: conn)
    transport("GET", "/v1/read/status")
    assert conn.calls[0][3]["Authorization"] == "Bearer fake-token"
    token.chmod(0o644)
    with pytest.raises(ValueError):
        transport("GET", "/v1/read/status")
    transport = EntryTransport({"AX_GATE_URL": "http://user:fake-token@example.invalid:8909"}, tmp_path)
    assert "fake-token" not in transport.target
    with pytest.raises(ValueError):
        transport("GET", "/v1/read/status")


def test_deferred_gate_dispatch_and_no_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(ax_cli, "ROOT", tmp_path)
    code, out, _, entry = cli(["--gate"])
    assert code == 1 and "b12b1" in json.loads(out)["say"] and not entry.calls
    library = tmp_path / "host/lib"
    library.mkdir(parents=True)
    (library / "ax_gate.py").write_text("def main(argv):\n    return 7 if argv == ['status'] else 1\n")
    assert cli(["--gate", "status"])[0] == 7


@pytest.mark.parametrize("argv,expected", [
    (["core", "change", "--spec", "-"], "bad_request"),
    (["seat", "add"], "bad_request"),
])
def test_unimplemented_operations_do_not_send_instructions(argv, expected):
    code, out, _, entry = cli(argv)
    assert code == 1 and not entry.calls
    assert json.loads(out)["error"]["code"] == expected


def test_control_retries_preserve_idempotency_payload(monkeypatch):
    from axcommon import schemas
    argv = ["control", "sl-demo", "run_now", "--reason", "点検", "--request-id", "same"]
    first = cli(argv)[3].calls[0][2]
    second = cli(argv)[3].calls[0][2]
    assert first["idempotency_key"] == second["idempotency_key"] == "same"
    assert first["payload"] == second["payload"]
    assert not schemas.errors("instruction", first)
    assert not schemas.errors("dest/control.command.json", first["payload"])


def test_exclude_resource_uses_until_and_release():
    values = '{"resource_id":"r","until":"2026-11-08T00:00:00.000Z"}'
    argv = ["control", "sl-demo", "exclude_resource", values, "--reason", "点検"]
    code, _, _, entry = cli(argv)
    assert code == 0
    body = entry.calls[0][2]
    assert body["expires_at"] == body["payload"]["args"]["until"]
    assert body["payload"]["on_expiry"] == "release"
    assert cli(argv + ["--lease", "30"])[0] == 1


def test_read_relay_schema(tmp_path):
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / "schemas/ext/b12/cli/read-relay.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    app, core, _ = relay_app(tmp_path)
    core.down = {"control"}
    _, body = request(app, "/v1/read/status", ident=LOCAL)
    Draft202012Validator(schema).validate(body)


@pytest.mark.parametrize("argv", [["--agent", "agent", "status"], ["--model", "model", "work", "--project", "p", "--title", "t"]])
def test_unspecified_metadata_is_not_silently_dropped(argv):
    code, out, _, entry = cli(argv)
    assert code == 1 and not entry.calls
    assert "未確定" in json.loads(out)["say"]


def test_oversized_entry_read_has_continuation_but_write_is_uncertain(tmp_path):
    huge = SimpleNamespace(status=200, read=lambda size: b"x" * size)
    conn = FakeConnection()
    conn.getresponse = lambda: huge
    transport = EntryTransport({"AX_SOCKET": "fake.sock"}, tmp_path, connection_factory=lambda path: conn)
    code, out, _, _ = cli(["status"], transport)
    result = json.loads(out)
    assert code == 0 and result["data"]["truncated"] and result["next"]
    with pytest.raises(ResultUnknown):
        transport("POST", "/v1/instructions", {})


def test_entry_launcher_only_dispatches(monkeypatch):
    import runpy
    monkeypatch.setattr(ax_cli, "main", lambda: 6)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "host/ax"), run_name="__main__")
    assert exc.value.code == 6


@pytest.mark.parametrize("status,nested,code", [
    (401, True, "unauthorized"), (403, True, "forbidden"),
    (401, False, "unauthorized"), (403, False, "forbidden"),
])
def test_control_error_reports_code_without_private_message(status, nested, code):
    error = {"code": code, "message": "private credential value"}
    response = {"ok": False, "result": {"error": error}} if nested else {"ok": False, "error": error}
    result, out, _, _ = cli(["control", "all", "set_mode", '{"mode":"draining"}', "--reason", "test"], FakeEntry(status, response))
    assert result == 6
    assert json.loads(out)["error"]["code"] == code
    assert "private credential value" not in out


def test_research_cli_sends_fixed_inputs_and_rejects_invalid():
    commit_id = 'a' * 40
    args = ['work', '--project', 'p', '--title', 't', '--role', 'research', '--body', 'inspect',
            '--target', 'repo', '--revision', commit_id, '--attachment', 'doc-1', '--attachment', 'doc-2']
    entry = FakeEntry()
    code, _, _, _ = cli(args, entry)
    assert code == 0 and len(entry.calls) == 1
    assert entry.calls[0][2]['payload'] == {'project': 'p', 'title': 't', 'role': 'research',
                                           'body': 'inspect', 'target': 'repo', 'revision': commit_id,
                                           'attachments': ['doc-1', 'doc-2']}
    for extra in [['--revision', 'abc'], ['--revision', 'A' * 40], ['--target', '../secret'],
                  ['--attachment', '/secret'], ['--verify', 'command']]:
        entry = FakeEntry()
        code, _, _, _ = cli(args + extra, entry)
        assert code != 0 and not entry.calls


def test_research_cli_requires_complete_fixed_input():
    for args in [
        ['work', '--project', 'p', '--title', 't', '--role', 'research'],
        ['work', '--project', 'p', '--title', 't', '--role', 'research', '--body', 'x', '--target', 'repo'],
        ['work', '--project', 'p', '--title', 't', '--role', 'impl', '--target', 'repo'],
    ]:
        entry = FakeEntry()
        code, _, _, _ = cli(args, entry)
        assert code != 0 and not entry.calls

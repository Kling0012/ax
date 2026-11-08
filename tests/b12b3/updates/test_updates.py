import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from core.intake.dest.b12b3.ledger_updates import handle
from host.lib.ax_ops.update import build_payload


@pytest.fixture
def invoke(monkeypatch):
    monkeypatch.setattr("core.intake.dest.b12b3.ledger_updates._token", lambda: "test-token")
    calls = []
    def transport(method, url, headers, body, timeout):
        calls.append((method, urlsplit(url).path, headers, json.loads(body)))
        return 200, {}, b'{"stored": true}'
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=transport)))
    def run(name, payload):
        return handle(app, {"name": name}, {"payload": payload}, None)
    return run, calls


@pytest.mark.parametrize("name,payload,method,path,expected", [
    ("goal.update", {"op":"create","id":"g1","body":"x","state":"active"}, "POST", "/v1/goals", {"id":"g1","body":"x","state":"active"}),
    ("goal.update", {"op":"update","id":"g1","version":1,"body":"y"}, "PATCH", "/v1/goals/g1", {"version":1,"body":"y"}),
    ("commitment.update", {"op":"transition","id":"p1","version":2,"to":"done","reason":"done"}, "POST", "/v1/promises/p1/transition", {"version":2,"to":"done","reason":"done"}),
    ("schedule.update", {"op":"update","id":"s1","version":1,"body":"y"}, "PATCH", "/v1/schedules/s1", {"version":1,"body":"y"}),
    ("life_state.update", {"kind":"focus","state":"focus","since":"2026-11-08T10:00:00+09:00","as_of":"2026-11-08T10:00:00+09:00"}, "PUT", "/v1/focus-state", {"state":"focus","since":"2026-11-08T10:00:00+09:00","as_of":"2026-11-08T10:00:00+09:00"}),
    ("life_state.update", {"kind":"daily","date":"2026-11-08","tz":"Asia/Tokyo","as_of":"2026-11-08T10:00:00+09:00","segments":[]}, "PUT", "/v1/daily-state/2026-11-08", {"date":"2026-11-08","tz":"Asia/Tokyo","as_of":"2026-11-08T10:00:00+09:00","segments":[]}),
])
def test_forward(invoke, name, payload, method, path, expected):
    run, calls = invoke
    assert run(name, payload)[0] == 200
    actual_method, actual_path, headers, data = calls[0]
    assert (actual_method, actual_path, data) == (method, path, expected)
    assert headers["Authorization"] == "Bearer test-token"


def test_invalid_payload_does_not_call_ledger(invoke):
    run, calls = invoke
    assert run("life_state.update", {"kind": "other"})[0] == 422
    assert calls == []


def test_downstream_conflict_is_preserved(monkeypatch):
    monkeypatch.setattr("core.intake.dest.b12b3.ledger_updates._token", lambda: "test-token")
    def transport(*args):
        return 409, {}, b'{"error":{"code":"stale_version"}}'
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=transport)))
    status, result = handle(app, {"name":"goal.update"}, {"payload":{"op":"update","id":"g","version":1,"body":"x"}}, None)
    assert status == 409
    assert result["error"]["code"] == "stale_version"


def test_ledger_unavailable_is_local_failure(monkeypatch):
    from axcommon.client import Unavailable
    monkeypatch.setattr("core.intake.dest.b12b3.ledger_updates._token", lambda: "test-token")
    def transport(*args):
        raise Unavailable()
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=transport)))
    assert handle(app, {"name":"goal.update"}, {"payload":{"op":"create","id":"g","body":"x","state":"active"}}, None)[0] == 503


@pytest.mark.parametrize("kind,name", [("goal","goal.update"),("commitment","commitment.update"),("life","life_state.update"),("schedule","schedule.update")])
def test_cli_payload(kind, name):
    assert build_payload(SimpleNamespace(kind=kind, payload='{"op":"create"}')) == (name, {"op":"create"})

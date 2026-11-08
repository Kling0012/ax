import json
from types import SimpleNamespace

import pytest

from axcommon.client import Unavailable
from core.intake.dest.b12b3 import todo_ops
from core.intake.vias import Ident
from host.lib.ax_ops import ledger_todo


def setup(status=200, response=None):
    calls = []
    def transport(method, url, headers, data, timeout):
        calls.append((method, url, headers, json.loads(data)))
        return status, {}, json.dumps(response if response is not None else {"state": "done"}).encode()
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=transport)))
    return app, calls


def ident(via="local"):
    return Ident(via, "hq" if via == "hq" else "commander", "principal", "operate")


DEST = {"handler": "http://ledger:8080/v1/todos/{todo_id}/transition"}


def test_complete_and_origin(monkeypatch):
    monkeypatch.setattr(todo_ops, "_token", lambda: "test-token")
    app, calls = setup()
    status, _ = todo_ops.complete(app, DEST, {"payload": {"todo_id": "a/b", "reason": "ok"}}, ident())
    assert status == 200
    assert calls == [("POST", "http://ledger:8080/v1/todos/a%2Fb/transition", {"Accept": "application/json", "X-AX-Via": "local", "Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer test-token"}, {"to": "done", "reason": "ok"})]


def test_park_requires_reason_and_time():
    app, calls = setup()
    assert todo_ops.park(app, DEST, {"payload": {"todo_id": "one", "reason": "wait"}}, ident())[0] == 422
    assert not calls
    assert todo_ops.park(app, DEST, {"payload": {"todo_id": "one", "reason": "wait", "check_at": "2026-11-08T00:00:00Z"}}, ident("hq"))[0] == 200
    assert calls[0][2]["X-AX-Via"] == "hq"
    assert calls[0][3] == {"to": "parked", "reason": "wait", "check_at": "2026-11-08T00:00:00Z"}


def test_rejection_is_preserved():
    app, _ = setup(403, {"error": {"code": "user_only_card", "message": "denied"}})
    status, result = todo_ops.complete(app, DEST, {"payload": {"todo_id": "x"}}, ident())
    assert status == 403 and result["error"]["code"] == "user_only_card"


def test_bad_origin_never_calls_ledger():
    app, calls = setup()
    assert todo_ops.complete(app, DEST, {"payload": {"todo_id": "x"}}, Ident("local", "hq", "x"))[0] == 403
    assert not calls


def test_unavailable_propagates():
    def fail(*args):
        raise Unavailable()
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(transport=fail)))
    with pytest.raises(Unavailable):
        todo_ops.complete(app, DEST, {"payload": {"todo_id": "x"}}, ident())


def test_cli_payloads():
    import argparse
    parser = argparse.ArgumentParser()
    ledger_todo.add_commands(parser.add_subparsers(dest="command", required=True))
    assert ledger_todo.request_payload(parser.parse_args(["done", "x"])) == ("ledger.complete", {"todo_id": "x"})
    assert ledger_todo.request_payload(parser.parse_args(["park", "x", "--reason", "wait", "--check-at", "2026-11-08T00:00:00Z"])) == ("ledger.park", {"todo_id": "x", "reason": "wait", "check_at": "2026-11-08T00:00:00Z"})

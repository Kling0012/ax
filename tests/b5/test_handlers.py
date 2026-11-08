"""最初の 2 つの行き先のハンドラ（core/intake/dest/b5/）と、登録簿だけの汎用の中継の試験。"""
from __future__ import annotations

from .helpers import HQ, LOCAL, client_for, events_of, instruction, make_app


def _control(fake_transport, tmp_path, **over):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.add("POST", "/v1/commands", json={"command_id": "c-1"})
    entry = {
        "name": "control.command",
        "title": "プラグインの制御",
        "schema": "schemas/dest/control.command.json",
        "handler": "http://control:8080/v1/commands",
        "allowed_from": ["commander", "hq"],
        "via": ["local", "ssh", "tailnet", "hq"],
        "tier": "operate",
        "irreversible": False,
        "ack": "sync",
        "default_expiry_min": 60,
        "conflict_key": "control:{target}:{op}:{args.name}",
        "mcp_tool": "ax_control",
    }
    entry.update(over)
    if entry.get("default_expiry_min") is None:
        entry.pop("default_expiry_min", None)
    return make_app(tmp_path, fake_transport, [entry])


def _ledger(fake_transport, tmp_path, **over):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.add("POST", "/v1/todos/td-9/answer", json={"ok": True})
    entry = {
        "name": "ledger.answer",
        "title": "ToDo カードへの回答",
        "schema": "schemas/dest/ledger.answer.json",
        "handler": "http://ledger:8080/v1/todos/{todo_id}/answer",
        "allowed_from": ["commander", "hq"],
        "via": ["local", "ssh", "tailnet", "hq"],
        "tier": "operate",
        "irreversible": False,
        "ack": "sync",
        "default_expiry_min": 60,
        "conflict_key": "card:{todo_id}",
        "mcp_tool": "ax_answer",
    }
    entry.update(over)
    return make_app(tmp_path, fake_transport, [entry])


def _control_payload(**over):
    p = {"target": "issue-automation", "op": "set_knob", "args": {"name": "n", "value": 1}, "reason": "試験"}
    p.update(over)
    return p


def test_control_command_sets_issued_by_commander_with_default_expiry(fake_transport, tmp_path, fake_clock, monkeypatch):
    monkeypatch.setenv("AX_SECRETS_PATH", str(tmp_path / "missing-secrets"))
    app = _control(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("control.command", _control_payload(),
                                                        idempotency_key="c-1"))
    assert r.status_code == 200
    call = [x for x in fake_transport.calls if x.path == "/v1/commands"][0]
    sent = call.json
    assert sent["issued_by"] == "commander"
    assert sent["expires_at"] == "2026-11-05T13:00:00.000Z"  # 既定の期限 60 分（default_expiry_min）
    assert sent["target"] == "issue-automation" and sent["op"] == "set_knob"
    # 資格が未設定の場合。偽の control は認証しないため、成功は認証成功を意味しない。
    assert "Authorization" not in call.headers


def test_control_command_uses_instruction_expiry_when_present(fake_transport, tmp_path, fake_clock):
    app = _control(fake_transport, tmp_path)
    with client_for(app) as c:
        c.post("/v1/instructions", json=instruction("control.command", _control_payload(), idempotency_key="c-2",
                                                    expires_at="2026-11-05T12:30:00.000Z"))
    sent = [x for x in fake_transport.calls if x.path == "/v1/commands"][0].json
    assert sent["expires_at"] == "2026-11-05T12:30:00.000Z"


def test_control_command_without_any_expiry_is_rejected(fake_transport, tmp_path):
    app = _control(fake_transport, tmp_path, default_expiry_min=None)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("control.command", _control_payload(), idempotency_key="c-3"))
    assert r.status_code == 422 and r.json()["result"]["error"]["code"] == "expiry_required"


def test_control_command_from_hq_sets_issued_by_user(fake_transport, tmp_path):
    app = _control(fake_transport, tmp_path)
    with client_for(app, HQ) as c:
        c.post("/v1/instructions", json=instruction("control.command", _control_payload(),
                                                    **{"from": "hq", "idempotency_key": "c-4"}))
    sent = [x for x in fake_transport.calls if x.path == "/v1/commands"][0].json
    assert sent["issued_by"] == "user"


def test_control_command_rejects_bad_payload(fake_transport, tmp_path):
    # schemas/dest/control.command.json が target/op/args/reason を必須とするため、
    # {target: "x"} は handler に届く前に入口の outer validation が拒否する（422 / error.code）。
    # expiry_required と違い handler 内の拒否として result 配下には来ない。
    app = _control(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("control.command", {"target": "x"}, idempotency_key="c-5"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "payload_schema"


def test_control_command_surfaces_control_rejection(fake_transport, tmp_path):
    app = _control(fake_transport, tmp_path)
    fake_transport.add("POST", "/v1/commands", status=403,  # 登録は後勝ち
                       json={"error": {"code": "control.rejected", "message": "範囲外"}})
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("control.command", _control_payload(), idempotency_key="c-6"))
    assert r.status_code == 403
    assert r.json()["status"] == 403 and r.json()["ok"] is False
    assert r.json()["result"]["error"]["code"] == "control.rejected"


def test_ledger_answer_relays_with_quote(fake_transport, tmp_path):
    app = _ledger(fake_transport, tmp_path)
    payload = {"todo_id": "td-9", "answer": "はい", "quote": "利用者の発言"}
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("ledger.answer", payload, idempotency_key="a-1"))
    assert r.status_code == 200
    call = [x for x in fake_transport.calls if x.path == "/v1/todos/td-9/answer"][0]
    assert call.json == {"answer": "はい", "answer_source": "relay", "quote": "利用者の発言"}
    routed = events_of(fake_transport, "instruction.routed")
    assert routed and routed[0]["data"]["destination"] == "ledger.answer"


def test_ledger_answer_requires_quote_for_relay(fake_transport, tmp_path):
    app = _ledger(fake_transport, tmp_path)
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("ledger.answer",
                                                        {"todo_id": "td-9", "answer": "はい"}, idempotency_key="a-2"))
    assert r.status_code == 422 and "quote" in r.json()["result"]["error"]["message"]


def test_ledger_answer_from_hq_uses_hq_source_without_quote(fake_transport, tmp_path):
    fake_transport.add("POST", "/v1/todos/td-9/answer", json={"ok": True})
    app = _ledger(fake_transport, tmp_path)
    with client_for(app, HQ) as c:
        c.post("/v1/instructions", json=instruction("ledger.answer", {"todo_id": "td-9", "answer": "本人の回答"},
                                                    **{"from": "hq", "idempotency_key": "a-3"}))
    call = [x for x in fake_transport.calls if x.path == "/v1/todos/td-9/answer"][0]
    assert call.json == {"answer": "本人の回答", "answer_source": "hq"}


def test_ledger_user_only_card_rejection_passes_through(fake_transport, tmp_path):
    """user_only のカードの拒否は台帳が行う（S-9.2）。入口はその応答をそのまま返す。"""
    app = _ledger(fake_transport, tmp_path)
    fake_transport.add("POST", "/v1/todos/td-9/answer", status=403,  # 登録は後勝ち
                       json={"error": {"code": "user_only_card", "message": "利用者本人しか答えられない"}})
    payload = {"todo_id": "td-9", "answer": "はい", "quote": "q"}
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("ledger.answer", payload, idempotency_key="a-4"))
    assert r.status_code == 403
    assert r.json()["result"]["error"]["code"] == "user_only_card"


def test_registry_only_destination_routes_by_url(fake_transport, tmp_path):
    """コードの handler を書かなくても、登録簿の handler の URL へ payload を渡す（T-6）。"""
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.add("POST", "/v1/notify", json={"sent": True})
    entry = {"name": "demo.notify", "title": "試験", "schema": "healthz",
             "handler": "http://demo:8081/v1/notify", "allowed_from": ["commander"], "via": ["local"],
             "tier": "read", "ack": "sync"}
    app = make_app(tmp_path, fake_transport, [entry])
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.notify",
                                                        {"ok": True, "component": "d", "version": "1"}))
    assert r.status_code == 200 and r.json()["result"] == {"sent": True}
    call = [x for x in fake_transport.calls if x.path == "/v1/notify"][0]
    assert call.json == {"ok": True, "component": "d", "version": "1"}


def test_non_http_handler_without_code_handler_is_not_routable(fake_transport, tmp_path):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    entry = {"name": "demo.self", "title": "試験", "schema": "healthz", "handler": "入口",
             "allowed_from": ["commander"], "via": ["local"], "tier": "read", "ack": "sync"}
    app = make_app(tmp_path, fake_transport, [entry])
    with client_for(app) as c:
        r = c.post("/v1/instructions", json=instruction("demo.self",
                                                        {"ok": True, "component": "d", "version": "1"}))
    assert r.status_code == 501 and r.json()["error"]["code"] == "handler_not_routable"


def test_async_destination_returns_pending_then_result(fake_transport, tmp_path):
    import time as _time

    fake_transport.add("POST", "/v1/events", json={"ok": True})

    def slow(call):
        _time.sleep(0.4)  # 最初の処理が終わる前に再送を届かせる
        return 200, {}, b'{"echoed": true}'

    fake_transport.add("POST", "/v1/echo", handler=slow)
    entry = {"name": "demo.async", "title": "試験", "schema": "healthz", "handler": "http://demo:8080/v1/echo",
             "allowed_from": ["commander"], "via": ["local"], "tier": "read", "ack": "async"}
    app = make_app(tmp_path, fake_transport, [entry])
    body = instruction("demo.async", {"ok": True, "component": "d", "version": "1"}, idempotency_key="as-1")
    with client_for(app) as c:
        r1 = c.post("/v1/instructions", json=body)
        assert r1.status_code == 202 and r1.json()["status"] == "pending" and r1.json()["replayed"] is False
        r2 = c.post("/v1/instructions", json=body)
        assert r2.status_code == 202 and r2.json()["status"] == "pending" and r2.json()["replayed"] is True
    deadline = _time.time() + 5
    while _time.time() < deadline:
        if len([x for x in fake_transport.calls if x.path == "/v1/echo"]) == 1:
            break
        _time.sleep(0.05)
    assert len([x for x in fake_transport.calls if x.path == "/v1/echo"]) == 1  # 二重に処理しない（S-6.16）

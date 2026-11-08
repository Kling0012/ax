"""T-49: control と ledger を通る管理役の設定提案。"""
import json

from axcommon.client import ApiClient
from tests.b9.helpers import in_min, reply
from tests.fakes.b9.world import TOK


def test_rejected_settings_produce_safe_deduplicated_cards(world):
    transport = world.net.transport("test")
    control = ApiClient("http://control:8080", TOK["core-intake"], transport=transport)
    ledger = ApiClient("http://ledger:8080", TOK["core-manager"], transport=transport)
    control.post("/v1/commands", {"target": "pa", "op": "set_knob", "args": {"name": "every_s", "value": 600},
                                  "reason": "user setting", "issued_by": "user"})
    before_pa = control.get("/v1/plugins/pa/effective")["knobs"]
    before_pool = control.get("/v1/plugins/worker-pool/effective")["knobs"]
    actions = [
        {"type": "control.issue", "command": {"target": "pa", "op": "set_knob", "args": {"name": "every_s", "value": 900}, "expires_at": in_min(30)}, "reason": "secret-reason-must-not-appear"},
        {"type": "control.issue", "command": {"target": "worker-pool", "op": "set_knob", "args": {"name": "family_cap", "value": 4}, "expires_at": in_min(30)}, "reason": "model cap"},
        {"type": "control.issue", "command": {"target": "pa", "op": "set_mode", "args": {"mode": "draining"}, "expires_at": in_min(600)}, "reason": "unrelated"},
    ]
    m = world.manager()
    for i in range(2):
        world.emit("error.classified", f"proposal-{i}", {"class": "unknown", "code": "x", "message": "x"}, component="pa")
        world.gateway.script(reply(*actions))
        m.fetch()
        m.run_cycle("event")
    assert control.get("/v1/plugins/pa/effective")["knobs"] == before_pa
    assert control.get("/v1/plugins/worker-pool/effective")["knobs"] == before_pool
    cards = [t for t in ledger.get("/v1/todos")["todos"] if t["state"] == "waiting_user"]
    assert len(cards) == 2
    assert {"every_s", "family_cap"} == {n for n in ("every_s", "family_cap") if any(n in t["card"]["question"] for t in cards)}
    assert "secret-reason-must-not-appear" not in json.dumps(cards)
    assert any("提案値: 900" in t["card"]["question"] for t in cards)
    assert any("提案値: 4" in t["card"]["question"] for t in cards)


def test_proposal_retries_when_ledger_is_unavailable(world):
    from core.manager.clients import ManagerLedger

    m = world.manager()
    m.c.ledger = ManagerLedger("http://ledger:8080", writes_configured=False, transport=world.net.transport("manager"))
    action = {"type": "control.issue", "command": {"target": "worker-pool", "op": "set_knob",
              "args": {"name": "family_cap", "value": 4}, "expires_at": in_min(30)}, "reason": "proposal"}
    world.emit("error.classified", "retry", {"class": "unknown", "code": "x", "message": "x"}, component="pa")
    world.gateway.script(reply(action))
    m.fetch()
    m.run_cycle("event")
    queued = m.exec.outbox.read_all()
    assert any(e["body"].get("state") == "waiting_user" for e in queued)
    m.c.ledger = world.clients("manager").ledger
    m.run_cycle("tick")
    ledger = ApiClient("http://ledger:8080", TOK["core-manager"], transport=world.net.transport("test"))
    cards = [t for t in ledger.get("/v1/todos")["todos"] if t["state"] == "waiting_user"]
    assert len(cards) == 1
    assert not m.exec.outbox.read_all()


def test_string_setting_value_and_reason_are_not_copied_to_card(world):
    control = ApiClient("http://control:8080", TOK["core-intake"], transport=world.net.transport("test"))
    control.post("/v1/commands", {"target": "pa", "op": "set_knob", "args": {"name": "model", "value": "flash"},
                                  "reason": "user setting", "issued_by": "user"})
    m = world.manager()
    world.emit("error.classified", "string-proposal", {"class": "unknown", "code": "x", "message": "x"}, component="pa")
    world.gateway.script(reply({"type": "control.issue", "command": {"target": "pa", "op": "set_knob",
                       "args": {"name": "model", "value": "standard"}, "expires_at": in_min(30)},
                       "reason": "credential-in-reason"}))
    m.fetch()
    m.run_cycle("event")
    ledger = ApiClient("http://ledger:8080", TOK["core-manager"], transport=world.net.transport("test"))
    cards = [t for t in ledger.get("/v1/todos")["todos"] if t["state"] == "waiting_user"]
    assert len(cards) == 1
    text = json.dumps(cards, ensure_ascii=False)
    assert "credential-in-reason" not in text and "standard" not in text
    assert "提案値は安全のため表示していません" in text
    assert control.get("/v1/plugins/pa/effective")["knobs"]["model"] == "flash"

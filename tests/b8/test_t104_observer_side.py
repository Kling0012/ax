"""T-104 のオブザーバー側: ホスト見張りのログ（hostwatch.log）の写し、sudo の不調のカード（同じ原因で 1 件）、配備の戻しの ToDo。"""
import json

import pytest

from tests.b8.helpers import Rig
from axcommon import ids


@pytest.fixture
def rig(tmp_path, fake_clock, fake_transport):
    return Rig(tmp_path, fake_clock, fake_transport)


def hw_event(kind, subject="x", data=None, ts="2026-11-05T12:00:00.000Z", **kw):
    return {"id": kw.pop("id", ids.new_ulid()), "ts": ts, "source": {"component": "hostwatch", "host": "h"}, "project": None, "subloop": None,
            "kind": kind, "subject": subject, "data": data or {}, "corr": None, "cause": None}


def append_log(rig, *evs, raw=()):
    with open(rig.state / "hostwatch.log", "ab") as f:
        for e in evs:
            f.write(json.dumps(e).encode() + b"\n")
        for r in raw:
            f.write(r + b"\n")


def test_host_log_lines_are_copied_to_the_timeline_once(rig):
    e1, e2 = hw_event("host.action", "up:timeline", {"op": "up", "target": "timeline", "result": "ok"}), hw_event("deploy.up", "ledger", {"result": "ok"})
    append_log(rig, e1, e2)
    rig.obs.run_once()
    assert [e["id"] for e in rig.tl.events if e["source"]["component"] == "hostwatch"] == [e1["id"], e2["id"]]
    rig.obs.run_once()
    assert rig.tl.posts == 2
    (rig.data / "hostlog.cursor").rename(rig.data / "hostlog.cursor.saved")
    rig.obs.run_once()
    assert len([e for e in rig.tl.events if e["source"]["component"] == "hostwatch"]) == 2  # 同じ id は 1 件


def test_host_log_waits_while_timeline_is_down_then_copies_in_order(rig):
    rig.tl.stop()
    e1, e2 = hw_event("host.action", "a", {"result": "ok"}), hw_event("host.action", "b", {"result": "ok"})
    append_log(rig, e1)
    rig.obs.run_once()
    append_log(rig, e2)
    rig.obs.run_once()
    rig.tl.start()
    rig.obs.run_once()
    assert [e["id"] for e in rig.tl.events if e["source"]["component"] == "hostwatch"] == [e1["id"], e2["id"]]


def test_broken_host_log_lines_are_skipped_and_partial_line_waits(rig):
    good = hw_event("host.action", "a", {"result": "ok"})
    append_log(rig, good, raw=[b"{broken", b"[1]"])
    with open(rig.state / "hostwatch.log", "ab") as f:
        f.write(b'{"id": "x"')  # 書きかけ
    rig.obs.run_once()
    assert [e["id"] for e in rig.tl.events if e["source"]["component"] == "hostwatch"] == [good["id"]]
    assert rig.obs.hostlog.last["invalid"] == 2


def test_sudo_unavailable_makes_exactly_one_user_card_per_cause(rig):
    append_log(rig, hw_event("host.sudo_unavailable", "sudo", {"result": "failed:1"}))
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1
    t = rig.ledger.todos[0]
    assert t["state"] == "waiting_user" and t["card"]["waiting_reason"] == "physical" and "sudo" in t["title"]
    # 同じ原因の事象が重なっても、周期を重ねても 1 件
    append_log(rig, hw_event("host.sudo_unavailable", "sudo", {"result": "failed:1"}))
    rig.obs.run_once()
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1
    # 戻ったら不調ではない。もう一度壊れたら新しい原因として 1 件
    append_log(rig, hw_event("host.sudo_restored", "sudo", {"result": "ok"}))
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1
    append_log(rig, hw_event("host.sudo_unavailable", "sudo", {"result": "failed:1"}))
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 2


def test_card_is_retried_when_the_ledger_is_down(rig):
    rig.ledger.stop()
    append_log(rig, hw_event("host.sudo_unavailable", "sudo", {"result": "failed:1"}))
    rig.obs.run_once()
    rig.obs.run_once()
    assert rig.ledger.todos == []
    rig.ledger.start()
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1


def test_other_functions_keep_running_while_sudo_is_unavailable(rig):
    """観測、台帳、入口は特権操作を要しない: sudo の不調でもオブザーバーは検査を回し続ける。"""
    append_log(rig, hw_event("host.sudo_unavailable", "sudo", {"result": "failed:1"}))
    rig.register("sl-a")
    rig.tl.set_rows("plugins", {"a": {}})
    rig.beat("a", age_s=999)
    rig.obs.run_once()
    assert len(rig.alerts("liveness")) == 1 and (rig.state / "observer.beat").exists()


def test_rolled_back_deploy_becomes_a_parked_todo(rig):
    append_log(rig, hw_event("deploy.rolled_back", "ledger", {"service": "ledger", "from_tag": "b1c2d3e4f5a6", "to_tag": "a0a0a0a0a0a0", "result": "ok"}))
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1
    t = rig.ledger.todos[0]
    assert t["state"] == "parked" and t["reason"] and t["check_at"] and "ledger" in t["title"]
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1


def test_rolled_back_todo_is_queued_while_ledger_is_down(rig):
    rig.ledger.stop()
    append_log(rig, hw_event("deploy.rolled_back", "ledger", {"service": "ledger", "from_tag": "n", "to_tag": "o"}))
    rig.obs.run_once()
    rig.ledger.start()
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1


def test_sudo_card_does_not_wait_for_timeline_recovery(rig):
    rig.tl.stop()
    append_log(rig, hw_event("host.sudo_unavailable", "sudo"))
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1
    rig.tl.start()
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 1
    assert len(rig.tl.of_kind("host.sudo_unavailable")) == 1


def test_log_replay_does_not_repeat_sudo_or_rollback_todos(rig):
    append_log(rig, hw_event("host.sudo_unavailable", "sudo"),
               hw_event("deploy.rolled_back", "ledger", {"service": "ledger"}))
    rig.obs.run_once()
    append_log(rig, hw_event("host.sudo_restored", "sudo"))
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 2
    (rig.data / "hostlog.cursor").write_text("{}")
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 2


def test_lost_ledger_reply_is_retried_with_same_todo_id(rig):
    from axcommon.client import Unavailable
    real_create = rig.obs.hostlog.ledger.create_todo
    def lost_reply(todo):
        real_create(todo)
        raise Unavailable("lost reply")
    rig.obs.hostlog.ledger.create_todo = lost_reply
    append_log(rig, hw_event("host.sudo_unavailable", "sudo"),
               hw_event("deploy.rolled_back", "ledger", {"service": "ledger"}))
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 2
    rig.obs.hostlog.ledger.create_todo = real_create
    rig.obs.run_once()
    rig.obs.run_once()
    assert len(rig.ledger.todos) == 2
    assert json.loads((rig.data / "sudo_card.json").read_text())["card"] is True
    assert json.loads((rig.data / "host_todos.json").read_text()) == []

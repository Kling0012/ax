"""T-13 管理役（S-10、D-9）。本物の timeline・control・ledger を FaultNet に載せ、モデルは b0 の FakeModelGateway（FakePool 経由）。

- 自分の expired_pending を最初に扱う。
- 形に合わない返答を捨てて、事象を台帳へ出す（error.classified(artifact.bug, bad_output)）。
- 判断の時間切れで事象を台帳へ出して、次へ進む。
- 禁止の操作（中核の制御、範囲の変更、サブループの作成、代理の回答）が拒否される。
"""
import json

from axcommon import timeutil
from tests.b9.helpers import in_min, noop, reply, seq_of
from tests.fakes.model_gateway import Delay, Reply


def _unknown(world, subject="x-1"):
    return world.emit("error.classified", subject, {"class": "unknown", "code": "weird", "message": "原因が分からない"}, component="pa")


def _expire_one(world, minutes=10):
    cmd = world.command("manager", target="pa", op="set_mode", args={"mode": "collect_only"}, reason="負荷を下げる",
                        expires_at=in_min(minutes), on_expiry="ask_issuer")
    world.clock.advance((minutes + 1) * 60)
    world.control.sweep_expiry()
    return cmd


def test_own_expired_pending_is_handled_first_then_the_inbox(world):
    cmd = _expire_one(world)
    unk = _unknown(world)
    world.gateway.script(reply({"type": "control.release", "command_id": cmd["id"], "reason": "もう要らない"}),
                         reply({"type": "ledger.eject", "subject": unk["id"], "reason": "判断できない", "check_at": in_min(60 * 24)}))
    m = world.manager()
    assert m.fetch() == 2                      # control.expired_pending（自分の命令）と unknown
    st = m.run_cycle("event")
    # 1 番目のモデル呼び出しが「自分の命令」、2 番目が受信箱
    assert "自分が出した制御命令" in world.pool.bodies[0]["prompt"] and cmd["id"] in world.pool.bodies[0]["prompt"]
    assert unk["id"] in world.pool.bodies[1]["prompt"] and cmd["id"] not in world.pool.bodies[1]["prompt"]
    assert all(b["role"] == "manager" for b in world.pool.bodies)
    # 命令は解除され、判断の順は commands → inbox
    assert world.control.store.get_command(cmd["id"])["state"] == "released"
    steps = [e["data"]["step"] for e in world.events("manager.decision")]
    assert steps == ["commands", "inbox"]
    assert seq_of(world, "control.released")[0] < world.events("manager.decision")[1]["seq"]
    # 受信箱の事象は片付き、周回の記録（manager.cycle）が 1 件、unknown は「扱った」印に載る
    assert m.store.counts() == {"handled": 2}
    cyc = world.events("manager.cycle")
    assert len(cyc) == 1 and unk["id"] in cyc[0]["data"]["handled"]
    assert st["errors"] == []


def test_renew_keeps_the_command_and_counts_the_renewal(world):
    cmd = _expire_one(world)
    new_exp = in_min(30)
    world.gateway.script(reply({"type": "control.renew", "command_id": cmd["id"], "expires_at": new_exp, "reason": "まだ負荷が高い"}))
    m = world.manager()
    m.fetch()
    m.run_cycle("event")
    c = world.control.store.get_command(cmd["id"])
    assert c["state"] == "active" and c["expires_at"] == new_exp and c["renewals"] == 1


def test_near_expiry_commands_are_reviewed_even_without_events(world):
    cmd = world.command("manager", target="pb", op="set_mode", args={"mode": "poll_paused"}, reason="様子見", expires_at=in_min(5), on_expiry="ask_issuer")
    world.gateway.script(reply(noop("まだ必要")))
    m = world.manager()
    m.run_cycle("tick")
    assert cmd["id"] in world.pool.bodies[0]["prompt"]


def test_bad_output_is_discarded_and_the_event_goes_to_the_ledger(world):
    unk = _unknown(world)
    world.gateway.script(Reply("これは JSON ではない"), Reply(json.dumps({"actions": [{"type": "explode"}]})),
                         Reply(json.dumps({"actions": "なし"})))
    m = world.manager()
    m.fetch()
    st = m.run_cycle("event")
    assert st["discarded"] == 1 and m.store.counts() == {"ejected": 1}
    err = world.events("error.classified", component="manager")
    assert len(err) == 1 and err[0]["data"]["class"] == "artifact.bug" and err[0]["data"]["code"] == "bad_output"
    assert world.events("manager.decision") == []                     # 捨てた判断は decision にしない（連続の数えを切らない）
    parked = [t for t in world.ledger.list_todos(state="parked") if t.get("source", {}).get("event_id") == unk["id"]]
    assert len(parked) == 1 and parked[0]["reason"] == "bad_output" and parked[0]["check_at"]
    assert unk["id"] in world.events("manager.cycle")[0]["data"]["ejected"]
    # 次の事象で、スキーマに合わない形（未知の type、actions が配列でない）も同じく捨てる
    for i in range(2):
        e = _unknown(world, f"x-{i + 2}")
        m.fetch()
        m.run_cycle("event")
    assert len(world.events("error.classified", component="manager")) == 3
    assert int(m.store.meta("bad_streak")) == 3


def test_a_valid_decision_resets_the_bad_streak(world):
    _unknown(world)
    world.gateway.script(Reply("壊れた"), reply(noop()))
    m = world.manager()
    m.fetch()
    m.run_cycle("event")
    assert int(m.store.meta("bad_streak")) == 1
    _unknown(world, "x-2")
    m.fetch()
    m.run_cycle("event")
    assert int(m.store.meta("bad_streak")) == 0 and len(world.events("manager.decision")) == 1


def test_decision_timeout_ejects_the_events_and_moves_on(world):
    unk = _unknown(world)
    world.gateway.script(Delay(10 ** 4, Reply("遅すぎる")))
    m = world.manager(decision_s=300)
    m.fetch()
    t0 = world.clock.monotonic()
    st = m.run_cycle("event")
    assert world.clock.monotonic() - t0 >= 300 and world.clock.monotonic() - t0 < 400          # 期限で打ち切った
    assert world.pool.cancelled == ["job-1"]                                                   # ジョブを取り消した
    assert m.store.counts() == {"ejected": 1}
    err = world.events("error.classified", component="manager")
    assert err[0]["data"]["code"] == "manager_timeout" and err[0]["data"]["class"] == "system.expected"
    parked = [t for t in world.ledger.list_todos(state="parked") if t["source"]["event_id"] == unk["id"]]
    assert parked and parked[0]["reason"] == "manager_timeout"
    assert len(world.events("manager.cycle")) == 1 and st["decisions"] == 0           # manager.cycle は書いて次へ進む
    # 次の周回は別の事象を普通に処理できる
    _unknown(world, "x-2")
    world.gateway.script(reply(noop()))
    m.fetch()
    m.run_cycle("event")
    assert len(world.events("manager.decision")) == 1


def test_model_unavailable_keeps_events_and_ejects_after_the_attempt_limit(world):
    unk = _unknown(world)
    world.pool.down()
    m = world.manager(attempts_max=3)
    m.fetch()
    m.run_cycle("event")
    m.run_cycle("event")
    assert m.store.counts() == {"new": 1}                       # 2 回までは、そのまま持つ
    m.run_cycle("event")
    assert m.store.counts() == {"ejected": 1}
    assert [t["reason"] for t in world.ledger.list_todos(state="parked")] == ["llm_unavailable:pool_unavailable"]
    assert len(world.events("manager.cycle")) == 3                # 落ちていても周回は書く
    assert unk["id"]


def test_forbidden_actions_are_rejected_by_the_policy_and_not_sent(world):
    unk = _unknown(world)
    ledger_id = None
    actions = [
        {"type": "control.issue", "command": {"target": "ledger", "op": "set_mode", "args": {"mode": "draining"}, "expires_at": in_min(30)}, "reason": "中核を止める"},
        {"type": "control.issue", "command": {"target": "timeline", "op": "set_mode", "args": {"mode": "draining"}, "expires_at": in_min(30)}, "reason": "中核"},
        {"type": "control.issue", "command": {"target": "pa", "op": "set_knob", "args": {"name": "max_parallel_items", "value": 8, "max": 100}, "expires_at": in_min(30)},
         "reason": "範囲を広げる"},
        {"type": "control.issue", "command": {"target": "pa", "op": "set_knob", "args": {"name": "max_parallel_items", "value": 3}, "expires_at": in_min(30)},
         "reason": "上限を上げる"},
        {"type": "control.issue", "command": {"target": "pa", "op": "set_mode", "args": {"mode": "run", "permission_ids": ["p1"]}, "expires_at": in_min(30)},
         "reason": "送信を再開（戻しにくい）"},
        {"type": "control.issue", "command": {"target": "pa", "op": "set_mode", "args": {"mode": "collect_only"}, "expires_at": in_min(600)}, "reason": "期限が長すぎる"},
        {"type": "control.issue", "command": {"target": "nope", "op": "set_mode", "args": {"mode": "collect_only"}, "expires_at": in_min(30)}, "reason": "未登録"},
        {"type": "ledger.add", "todo": {"title": "代理の回答", "state": "waiting_user", "owner": "user",
                                        "card": {"question": "公開してよい？", "answer": "はい", "waiting_reason": "preference"}}, "reason": "代わりに答える"},
        {"type": "ledger.add", "todo": {"title": "完了にする", "state": "done"}, "reason": "勝手に完了"},
        {"type": "control.release", "command_id": "01JNOTMINE0000000000000000", "reason": "他人の命令"},
    ]
    world.gateway.script(reply(*actions))
    m = world.manager()
    m.fetch()
    st = m.run_cycle("event")
    dec = world.events("manager.decision")[0]["data"]
    codes = {r["code"] for r in dec["rejected"]}
    assert len(dec["rejected"]) == len(actions) and st["executed"] == 0
    assert {"core_control_forbidden", "range_change_forbidden", "limit_change_forbidden", "irreversible_forbidden", "lease_too_long",
            "unknown_target", "proxy_answer_forbidden", "todo_state", "not_own_command"} <= codes
    # 制御 API へは 1 件も届いていない（POST /v1/commands なし）。命令は増えていない
    posts = [c for c in world.net.calls_to("control") if c.method == "POST" and c.path == "/v1/commands"]
    assert posts == [] and world.control.list_commands() == []
    assert [t for t in world.ledger.list_todos() if t["title"] in ("代理の回答", "完了にする")] == []
    # 許される操作が 1 つも無かったので、判断できなかったのと同じ扱い（台帳へ出す）
    assert m.store.counts() == {"ejected": 1}
    assert [t["reason"] for t in world.ledger.list_todos(state="parked")] == ["manager_forbidden_action"]


def test_control_api_also_refuses_core_targets_even_if_the_policy_were_bypassed(world):
    """二重の防御: 管理役の検査を通さずに制御 API を直接呼んでも、中核・未登録の target は拒否される（S-5.4 の 1）。"""
    import pytest
    from axcommon.client import ApiError

    for tgt in ("ledger", "timeline", "nope"):
        with pytest.raises(ApiError) as ei:
            world.command("manager", target=tgt, op="set_mode", args={"mode": "draining"}, reason="x", expires_at=in_min(10), on_expiry="release")
        assert ei.value.status == 400 and ei.value.code == "unknown_target"
    assert len(world.events("control.rejected")) == 3             # 拒否は control.rejected に残る（S-5.4）
    assert world.control.list_commands() == []


def test_subloop_creation_is_not_in_the_allowed_actions_and_the_whole_reply_is_discarded(world):
    unk = _unknown(world)
    world.gateway.script(reply({"type": "subloop.create", "spec": {"id": "new-loop"}, "reason": "新しいループ"}, noop()))
    m = world.manager()
    m.fetch()
    st = m.run_cycle("event")
    assert st["discarded"] == 1 and world.events("error.classified", component="manager")[0]["data"]["code"] == "bad_output"
    assert not (world.axroot / "plugins" / "subloop" / "new-loop").exists()
    assert [t["reason"] for t in world.ledger.list_todos(state="parked")] == ["bad_output"]


def test_allowed_actions_are_executed_in_the_defined_order_and_recorded(world):
    unk = _unknown(world)
    world.gateway.script(reply(
        {"type": "ledger.add", "todo": {"title": "ループ化の提案", "state": "waiting_user", "owner": "user",
                                        "card": {"question": "この作業をループにしますか", "options": ["する", "しない"], "waiting_reason": "preference"}},
         "reason": "同じ依頼が続く"},
        {"type": "control.issue", "command": {"target": "pa", "op": "set_mode", "args": {"mode": "collect_only"}, "expires_at": in_min(30)}, "reason": "崩れの緩和"},
        {"type": "timer.create", "timer": {"action": "plugin.run_now", "target": "pa", "at": in_min(40)}, "reason": "戻ったら 1 回回す"}))
    m = world.manager()
    m.fetch()
    st = m.run_cycle("event")
    dec = world.events("manager.decision")[0]["data"]
    assert [r["type"] for r in dec["executed"]] == ["control.issue", "timer.create", "ledger.add"] and all(r["ok"] for r in dec["executed"])
    cmds = world.control.list_commands()
    assert len(cmds) == 1 and cmds[0]["issued_by"] == "manager" and cmds[0]["on_expiry"] == "ask_issuer" and cmds[0]["reason"] == "崩れの緩和"
    assert len(world.control.list_timers()) == 1
    card = [t for t in world.ledger.list_todos() if t["title"] == "ループ化の提案"][0]
    assert card["state"] == "waiting_user" and card["created_by"] == "manager"
    assert unk["id"] in dec["handled"] and st["executed"] == 3


def test_unknown_with_only_noop_is_ejected_not_left_unhandled(world):
    unk = _unknown(world)
    world.gateway.script(reply(noop("様子を見る")))
    m = world.manager()
    m.fetch()
    m.run_cycle("event")
    assert m.store.counts() == {"ejected": 1}
    assert [t["reason"] for t in world.ledger.list_todos(state="parked")] == ["unknown_unresolved"]
    cyc = world.events("manager.cycle")[0]["data"]
    assert unk["id"] in cyc["handled"]


def test_late_success_is_timed_out_before_actions_are_executed(world):
    unk = _unknown(world)
    world.gateway.script(reply({"type": "control.issue", "command": {"target": "pa", "op": "set_mode",
                               "args": {"mode": "draining"}, "expires_at": in_min(30)}, "reason": "遅れた判断"}))
    m = world.manager(decision_s=3)
    original_job = world.pool.job

    def late_job(job_id):
        world.clock.advance(4)
        return original_job(job_id)

    world.pool.job = late_job
    m.fetch()
    st = m.run_cycle("event")
    assert world.pool.cancelled == ["job-1"]
    assert world.control.list_commands() == [] and st["decisions"] == 0
    assert m.store.state_of(unk["id"]) == "ejected"
    assert world.ledger.list_todos(state="parked")[0]["reason"] == "manager_timeout"


def test_polling_and_http_waits_fit_the_remaining_decision_budget(fake_clock, fake_transport):
    import pytest
    from axcommon.client import WorkerPoolClient
    from core.manager.llm import DecisionTimeout, PoolLLM
    from tests.fakes.fault import FaultNet

    fake_transport.add("POST", "/v1/jobs", json={"job_id": "j1"})
    fake_transport.add("GET", "/v1/jobs/j1", json={"state": "ok", "report": {"text": "late"}})
    fake_transport.add("POST", "/v1/jobs/j1/cancel", json={"ok": True})
    net = FaultNet(fake_clock).add("worker-pool", fake_transport)
    net.delay("worker-pool", 10, method="GET")
    pool = WorkerPoolClient("http://worker-pool:8080", token="test-manager-token", transport=net.transport("manager"))
    llm = PoolLLM(pool, poll_s=5, sleep=fake_clock.sleep, monotonic=fake_clock.monotonic)
    with pytest.raises(DecisionTimeout):
        llm.ask("test", sensitivity="private", deadline_s=0.25)
    assert fake_clock.monotonic() == 0.25
    assert fake_transport.paths() == ["/v1/jobs", "/v1/jobs/j1/cancel"]
    assert pool.timeout == 10.0


def test_polling_sleep_does_not_overrun_a_short_decision_budget(world):
    _unknown(world)
    world.pool.stuck_next()
    m = world.manager(decision_s=0.25)
    m.fetch()
    before = world.clock.monotonic()
    m.run_cycle("event")
    assert world.clock.monotonic() - before == 0.25
    assert world.pool.cancelled == ["job-1"]


def test_prompt_includes_manifest_bounds_resources_and_other_issuers_commands(world):
    other = world.command("inspection", target="pb", op="set_mode", args={"mode": "collect_only"}, reason="点検の制御",
                          expires_at=in_min(30), on_expiry="release")
    world.resources.items = [{"id": "glm-test", "kind": "model", "status": "excluded", "tier": "standard"}]
    _unknown(world)
    world.gateway.script(reply(noop()))
    m = world.manager()
    m.fetch()
    m.run_cycle("event")
    prompt = world.pool.last_prompt
    assert other["id"] in prompt and '"issued_by": "inspection"' in prompt
    assert '"capabilities": [' in prompt and '"knob_specs"' in prompt and '"min": 60' in prompt
    assert '"id": "glm-test"' in prompt and '"status": "excluded"' in prompt
    assert all(body["role"] == "manager" and "pin_model" not in body for body in world.pool.bodies)


def test_ledger_rejection_keeps_the_event_pending_until_it_can_be_parked(world):
    unk = _unknown(world)
    world.gateway.script(reply(noop()), reply(noop()))
    world.net.respond("ledger", 403, {"error": {"code": "forbidden"}}, method="POST", path="/v1/todos", times=1)
    m = world.manager()
    m.fetch()
    first = m.run_cycle("event")
    assert m.store.state_of(unk["id"]) == "new" and first["handled"] == [] and first["ejected"] == []
    assert world.events("manager.decision")[0]["data"]["handled"] == []
    m.run_cycle("event")
    assert m.store.state_of(unk["id"]) == "ejected"
    assert len(world.ledger.list_todos(state="parked")) == 1


def test_rejected_ejection_is_not_recorded_as_handled(world):
    unk = _unknown(world)
    world.gateway.script(reply({"type": "ledger.eject", "subject": unk["id"], "reason": "過去の時刻", "check_at": in_min(-1)}, noop()))
    m = world.manager()
    m.fetch()
    m.run_cycle("event")
    assert m.store.state_of(unk["id"]) == "ejected"
    assert world.ledger.list_todos(state="parked")[0]["reason"] == "unknown_unresolved"


def test_sensitive_event_without_an_eligible_model_is_parked_without_a_model_call(world):
    event = world.emit("error.classified", "sensitive", {"class": "unknown", "code": "x", "message": "限定情報", "sensitivity": "sensitive"})
    world.resources.items = [{"id": "free-test", "kind": "model", "tier": "free", "status": "active"}]
    m = world.manager()
    m.fetch()
    st = m.run_cycle("event")
    assert world.pool.bodies == []
    assert m.store.state_of(event["id"]) == "parked"
    assert event["id"] in st["handled"]
    assert world.ledger.list_todos(state="parked")[0]["reason"] == "no_sensitive_model"


def test_redaction_runs_before_shortening_indexed_values():
    from core.manager.summary import summarize_event

    value = "synthetic-private-value-" * 40
    def redact(obj):
        if isinstance(obj, dict):
            return {k: redact(v) for k, v in obj.items()}
        return "[REDACTED]" if obj == value else obj

    view = summarize_event({"kind": "error.classified", "data": {"message": value}}, str_max=20, redact=redact)
    assert view["data"]["message"] == "[REDACTED]"


def test_sensitive_parked_rechecks_keep_their_original_information_class(world):
    event = world.emit("error.classified", "sensitive-only-subject", {"class": "unknown", "code": "x", "sensitivity": "sensitive"})
    m = world.manager()
    m.fetch()
    m.run_cycle("event")
    assert m.store.state_of(event["id"]) == "parked" and world.pool.bodies == []
    world.clock.advance(3601)
    _unknown(world, "ordinary")
    world.gateway.script(reply(noop()))
    m.fetch()
    m.run_cycle("tick")
    assert len(world.pool.bodies) == 1
    assert world.pool.bodies[0]["sensitivity"] == "private"
    assert "sensitive-only-subject" not in world.pool.bodies[0]["prompt"]
    world.resources.items = [{"id": "glm-test", "kind": "model", "tier": "standard", "status": "active"}]
    world.gateway.script(reply(noop()))
    m.run_cycle("tick")
    assert world.pool.bodies[-1]["sensitivity"] == "sensitive"
    assert "sensitive-only-subject" in world.pool.bodies[-1]["prompt"]


def test_forbidden_model_result_is_not_applied_or_retried(world):
    event = _unknown(world)
    world.pool.model = "codex/gpt-test"
    world.gateway.script(reply({"type": "control.issue", "command": {"target": "pa", "op": "set_mode",
                               "args": {"mode": "draining"}, "expires_at": in_min(30)}, "reason": "適用しない"}))
    m = world.manager()
    m.fetch()
    m.run_cycle("event")
    assert world.control.list_commands() == []
    assert world.pool.cancelled == ["job-1"]
    assert m.store.state_of(event["id"]) == "ejected"
    assert world.ledger.list_todos(state="parked")[0]["reason"] == "manager_model_forbidden"
    m.run_cycle("tick")
    assert len(world.pool.bodies) == 1


def test_unconfigured_ledger_authority_buffers_only_the_write(world):
    from core.manager.clients import ManagerLedger

    event = _unknown(world)
    world.gateway.script(reply(noop()))
    m = world.manager()
    m.c.ledger = ManagerLedger("http://ledger:8080", writes_configured=False, transport=world.net.transport("manager"))
    m.fetch()
    before = len(world.net.calls_to("ledger"))
    m.run_cycle("event")
    assert m.store.state_of(event["id"]) == "ejected" and len(m.exec.outbox) == 1
    assert not [c for c in world.net.calls_to("ledger")[before:] if c.method == "POST"]
    m.c.ledger = world.clients("manager").ledger
    m.run_cycle("tick")
    assert len(m.exec.outbox) == 0 and len(world.ledger.list_todos(state="parked")) == 1


def test_repeated_resource_failure_is_mitigated_before_creating_one_repair_todo(world):
    until = in_min(30)
    data = {"resource_id": "model-test", "code": "unavailable", "count": 4}
    event = world.emit("resource.repeat_failure", "model-test", data, component="worker-pool")
    world.gateway.script(reply({"type": "control.issue", "command": {"target": "worker-pool", "op": "exclude_resource",
                               "args": {"resource_id": "model-test", "until": until}, "expires_at": until, "on_expiry": "release"},
                               "reason": "繰り返す障害を緩和する"}))
    m = world.manager()
    m.fetch()
    first = m.run_cycle("event")
    commands = world.control.list_commands()
    assert len(commands) == 1 and commands[0]["op"] == "exclude_resource"
    result = world.events("manager.decision")[0]["data"]["executed"][0]
    assert result["ok"] is True
    assert first["handled"] == [event["id"]]
    assert m.store.pending_count() == 0
    assert first["repairs"] == ["恒久修繕: model-test unavailable"]
    assert seq_of(world, "control.issued")[0] < seq_of(world, "task.state")[0]
    world.emit("resource.repeat_failure", "model-test", {**data, "count": 5}, component="worker-pool")
    world.gateway.script(reply(noop()))
    m.fetch()
    m.run_cycle("event")
    assert len([t for t in world.ledger.list_todos() if t["title"] == "恒久修繕: model-test unavailable"]) == 1


def test_sensitive_event_uses_a_separate_manager_job_with_the_same_classification(world):
    event = world.emit("error.classified", "sensitive", {"class": "unknown", "code": "x", "message": "限定情報", "sensitivity": "sensitive"})
    world.resources.items = [{"id": "zhipu/glm-test", "kind": "model", "tier": "standard", "status": "active"}]
    world.gateway.script(reply(noop()))
    m = world.manager()
    m.fetch()
    m.run_cycle("event")
    assert len(world.pool.bodies) == 1 and world.pool.bodies[0]["sensitivity"] == "sensitive"
    assert world.pool.bodies[0]["role"] == "manager"
    assert m.store.state_of(event["id"]) == "ejected"


def test_repeated_failure_creates_one_permanent_repair_per_resource_and_code(world):
    for _ in range(2):
        world.emit("resource.repeat_failure", "model-x", {"resource_id": "model-x", "code": "quota", "count": 4}, component="worker-pool")
    world.gateway.script(reply(noop()))
    m = world.manager()
    m.fetch()
    first = m.run_cycle("event")
    repairs = [t for t in world.ledger.list_todos() if t["title"] == "恒久修繕: model-x quota"]
    assert len(repairs) == 1 and len(first["repairs"]) == 1
    world.emit("resource.repeat_failure", "model-x", {"resource_id": "model-x", "code": "quota", "count": 5}, component="worker-pool")
    world.gateway.script(reply(noop()))
    m.fetch()
    m.run_cycle("event")
    assert len([t for t in world.ledger.list_todos() if t["title"] == "恒久修繕: model-x quota"]) == 1

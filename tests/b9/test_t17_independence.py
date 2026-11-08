"""T-17 独立性（B13。S-2.2、F-18.4）。故障注入の偽（tests/fakes/fault）で、1 つ壊しても他が続くことを確かめる。

(a) 任意のプラグインを 1 つ壊しても、他のプラグインの周回が続く（壊し方 5 種 × 3 プラグイン）。
(b) 中核の部品を 1 つ落としても、他の中核は記録を溜めて待ち、戻ったら続きを処理する（timeline、ledger、control）。
(c) 時系列記録を止めても、サブループは outbox/ に溜めて周回を続け、戻ったら送る（作業側の口 8081）。
本物の timeline・control・ledger と B7 の実行系を使う。ソケットは使わない。
"""
import asyncio

import pytest

from axcommon import ids
from axcommon.client import Unavailable
from tests.b9.helpers import in_min, noop, reply
from tests.fakes.fault import Chaos, run_rounds

PLUGINS = ("pa", "pb", "pc")
MODES = ("crash", "hang", "apply_down", "timeline_down_for_it", "bad_events")
ROUNDS = 3


def run(coro):
    return asyncio.run(coro)


def _runner(world, pid):
    ex = world.runtimes[pid]["ex"]

    async def one():
        await ex.run_once()
        await ex.drain()
        await asyncio.to_thread(world.runtimes[pid]["sink"].flush)

    return one


def _break(world, chaos, pid, mode):
    ex = world.runtimes[pid]["ex"]
    if mode == "crash":
        chaos.raise_on(ex, "run_once", RuntimeError("boom"))
    elif mode == "hang":
        chaos.hang(ex, "run_once")
    elif mode == "apply_down":
        world.net.stop(f"sl-{pid}")
    elif mode == "timeline_down_for_it":
        world.net.stop("timeline", caller=pid)
    elif mode == "bad_events":
        # 許されない kind（入口だけが書ける）を書き続ける。timeline:8081 は events.rejected にして拒否し、プラグインは先へ進む
        sink = world.runtimes[pid]["sink"]
        chaos.replace(sink, "emit", lambda orig, kind, subject, data=None, **kw: orig("instruction.received", subject, data, **kw))
    else:
        raise AssertionError(mode)


@pytest.mark.parametrize("broken,mode", [(p, m) for p in PLUGINS for m in MODES])
def test_breaking_any_one_plugin_leaves_the_others_running(world, broken, mode):
    for pid in PLUGINS:
        world.add_plugin_runtime(pid, items=2)
    chaos = Chaos(world.clock)
    _break(world, chaos, broken, mode)
    # 壊れたプラグインがあっても、管理役は他のプラグインを制御でき、命令は届く
    healthy = [p for p in PLUGINS if p != broken]
    cmd = world.command("manager", target=healthy[0], op="set_knob", args={"name": "model", "value": "free"}, reason="試験", expires_at=in_min(10),
                        on_expiry="release")
    assert cmd["state"] == "active"
    stats = run(run_rounds({p: _runner(world, p) for p in PLUGINS}, n=ROUNDS, timeout_s={p: (0.3 if p == broken else 20.0) for p in PLUGINS}))
    for p in healthy:
        assert stats[p].ok == ROUNDS and stats[p].failed == 0 and stats[p].timed_out == 0, (p, stats[p])
        runs = world.runtimes[p]["ex"].store.runs()
        assert len(runs) >= 1 and all(r["state"] in ("completed", "skipped") for r in runs), runs
        assert all(i["state"] == "completed" for i in world.runtimes[p]["ex"].store.items() if not i["item_id"].startswith("run:"))
    if mode in ("crash", "hang"):
        assert stats[broken].ok == 0 and (stats[broken].failed + stats[broken].timed_out) == ROUNDS      # 壊れた分だけが失敗する
    # 壊れたプラグインの故障は、他のプラグインの事象にも命令にも伝わらない
    ev_by = {}
    for e in world.events("run.state"):
        ev_by.setdefault(e["source"]["component"], []).append(e)
    for p in healthy:
        assert ev_by.get(p), p
    if mode == "bad_events":
        rej = world.events("events.rejected")
        assert rej and {e["subject"] for e in rej} == {broken}                         # 拒否は壊れたプラグインの分だけ
        assert not [e for e in world.events("instruction.received") if e["source"]["component"] == broken]
    chaos.restore()
    world.net.clear()
    # 直ったら、壊れていたプラグインも普通に周回を再開する
    again = run(run_rounds({broken: _runner(world, broken)}, n=1, timeout_s=20.0))
    assert again[broken].ok == 1


def test_manager_keeps_cycling_when_a_plugin_is_down_and_its_commands_still_reach_the_healthy(world):
    for pid in PLUGINS:
        world.add_plugin_runtime(pid)
    world.net.stop("sl-pa")
    world.emit("health.alert", "liveness:pa", {"check": "liveness", "target": "pa", "severity": "high", "destination": "manager"}, component="observer")
    world.gateway.script(reply(
        {"type": "control.issue", "command": {"target": "pa", "op": "set_mode", "args": {"mode": "collect_only"}, "expires_at": in_min(30)}, "reason": "pa が落ちた"},
        {"type": "control.issue", "command": {"target": "pb", "op": "set_mode", "args": {"mode": "collect_only"}, "expires_at": in_min(30)}, "reason": "念のため"},
    ))
    m = world.manager()
    m.fetch()
    st = m.run_cycle("event")
    dec = world.events("manager.decision")[0]["data"]
    assert [r["ok"] for r in dec["executed"]] == [True, True]                      # 命令の発行は pa が落ちていても成功する（効力は effective に残る）
    assert world.runtimes["pb"]["ex"].mode == "collect_only"                       # 届く先には届く
    assert world.runtimes["pa"]["ex"].mode == "run"
    world.net.start("sl-pa")
    world.poll_effective("pa")
    assert world.runtimes["pa"]["ex"].mode == "collect_only"                       # 戻ったら effective を読んで合う


# ---------------------------------------------------------------- (b) 中核の部品を 1 つ落とす
def _flush_all(*sinks):
    for s in sinks:
        s.flush()


def test_timeline_down_others_buffer_and_continue_then_resume_in_order(world):
    m = world.manager()
    insp = world.inspector()
    world.net.stop("timeline")
    # 制御 API: 命令は受け付ける。事象は outbox に溜まる
    c1 = world.command("manager", target="pa", op="set_mode", args={"mode": "collect_only"}, reason="x", expires_at=in_min(20), on_expiry="release")
    assert c1["state"] == "active"
    n_ctl = len(world.control.sink.outbox)
    assert n_ctl >= 1
    # 台帳: ToDo を作れる（事象は溜まる）
    t = world.ledger.create_todo({"title": "timeline 停止中の ToDo", "state": "todo", "owner": "system"}, actor="manager")
    assert t["id"] and len(world.ledger.sink.outbox) >= 1
    # 管理役: 購読は読めないが周回は動き、manager.cycle を溜める。受信箱の事象は無いので判断はしない
    assert m.fetch() == 0 and m.timeline_ok is False
    st = m.run_cycle("tick")
    assert st["errors"] == [] and len(m.sink.outbox) >= 1
    # 点検係も止まらない
    assert insp.run_once() == [] and insp.timeline_ok is False
    # timeline が戻ったら、溜めた分が順に届く
    world.net.start("timeline")
    _flush_all(world.control.sink, world.ledger.sink, m.sink, insp.sink)
    assert len(world.control.sink.outbox) == len(world.ledger.sink.outbox) == len(m.sink.outbox) == 0
    kinds = [e["kind"] for e in world.events()]
    assert "control.issued" in kinds and "task.state" in kinds and "manager.cycle" in kinds
    seqs_ids = [e["id"] for e in world.events()]
    assert len(seqs_ids) == len(set(seqs_ids))                                  # 二重にならない
    # 戻った後は、管理役が購読を再開できる
    world.emit("error.classified", "x-1", {"class": "unknown", "code": "c", "message": "m"}, component="pa")
    assert m.fetch() == 1 and m.timeline_ok is True


def test_ledger_down_manager_queues_ejections_and_sends_them_when_it_returns(world):
    unk = world.emit("error.classified", "x-1", {"class": "unknown", "code": "weird", "message": "?"}, component="pa")
    world.gateway.script(reply(noop("様子を見る")))
    m = world.manager()
    m.fetch()
    world.net.stop("ledger")
    st = m.run_cycle("event")
    assert st["errors"] == []                                                   # 台帳が落ちていても周回は止まらない
    assert m.store.counts() == {"ejected": 1}                                   # 台帳へ出す依頼は自分のボリュームに溜まった
    assert len(m.exec.outbox) == 1
    # 台帳が戻る → 次の周回で溜めた分を送る
    world.net.start("ledger")
    m.run_cycle("tick")
    assert len(m.exec.outbox) == 0
    parked = world.ledger.list_todos(state="parked")
    assert len(parked) == 1 and parked[0]["source"]["event_id"] == unk["id"]
    # 同じ周回で 2 回送られない
    m.run_cycle("tick")
    assert len(world.ledger.list_todos(state="parked")) == 1


def test_control_down_manager_skips_the_control_steps_and_others_continue(world):
    unk = world.emit("error.classified", "x-1", {"class": "unknown", "code": "weird", "message": "?"}, component="pa")
    world.gateway.script(reply({"type": "control.issue", "command": {"target": "pa", "op": "set_mode", "args": {"mode": "collect_only"}, "expires_at": in_min(20)},
                                "reason": "緩和"}))
    m = world.manager(attempts_max=3)
    m.fetch()
    world.net.stop("control")
    st = m.run_cycle("event")
    assert st["errors"] == []
    dec = world.events("manager.decision")[0]["data"]
    assert dec["executed"][0].get("unavailable") is True                         # 命令は出せなかったと記録する
    assert m.store.counts() == {"new": 1}                                        # 事象は手元に残し、次の周回でやり直す
    world.net.start("control")
    world.gateway.script(reply({"type": "control.issue", "command": {"target": "pa", "op": "set_mode", "args": {"mode": "collect_only"}, "expires_at": in_min(20)},
                                "reason": "緩和"}))
    m.run_cycle("event")
    assert m.store.counts() == {"handled": 1} and len(world.control.list_commands()) == 1
    # 台帳とタイムラインは control が落ちている間も使えた
    assert world.ledger.list_todos() == []                                       # この件は台帳に出す必要がなかった


def test_inspector_waits_for_control_and_does_not_skip_the_drain(world):
    insp = world.inspector()
    from tests.b9.test_t16_inspection import _to_notice, _to_start

    _to_notice(world, insp)
    insp.run_once()
    world.net.stop("control")
    _to_start(world, insp)
    assert insp.tick() == [] and insp.state["phase"] == "notice"                  # 減らし込みを出せない間は進めない（早まって見直しに進まない）
    world.net.start("control")
    assert insp.tick()[0] == "draining" and len(world.control.list_commands()) == 4


def test_inspector_gives_up_this_round_if_control_stays_down(world):
    insp = world.inspector()
    from tests.b9.test_t16_inspection import _to_notice, _to_start

    _to_notice(world, insp)
    insp.run_once()
    world.net.stop("control")
    _to_start(world, insp)
    insp.tick()
    world.clock.advance(insp.cfg.drain_wait_h * 3600 + 5)
    assert insp.tick() == ["resume"] and insp.state["last_result"]["resume_reason"] == "control_unavailable"
    assert insp.state["phase"] == "idle" and insp.state["last_result"]["quiet"] is False


# ---------------------------------------------------------------- (c) 時系列記録を止める（サブループ）
def test_timeline_down_subloops_keep_cycling_with_outbox_and_send_when_it_returns(world):
    for pid in ("pa", "pb"):
        world.add_plugin_runtime(pid)
    world.net.stop("timeline")
    stats = run(run_rounds({p: _runner(world, p) for p in ("pa", "pb")}, n=ROUNDS, timeout_s=20.0))
    for p in ("pa", "pb"):
        assert stats[p].ok == ROUNDS                                              # 周回は続く
        assert len(world.runtimes[p]["sink"].outbox) > 0                          # 事象は outbox/ に溜まる
        assert (world.tmp / f"rt-{p}" / "outbox" / "pending.jsonl").exists()
    before = {p: len(world.runtimes[p]["sink"].outbox) for p in ("pa", "pb")}
    assert world.events("run.state") == []
    world.net.start("timeline")
    run(run_rounds({p: _runner(world, p) for p in ("pa", "pb")}, n=1, timeout_s=20.0))
    for p in ("pa", "pb"):
        assert len(world.runtimes[p]["sink"].outbox) == 0                         # 戻ったら順に送る
    rs = world.events("run.state")
    assert {e["source"]["component"] for e in rs} == {"pa", "pb"}
    seen = [e["id"] for e in rs]
    assert len(seen) == len(set(seen)) and all(sum(1 for e in rs if e["source"]["component"] == p) >= 2 for p in ("pa", "pb")), before
    # 溜めた順に届く（同じプラグインの run.state の時刻が単調）
    for p in ("pa", "pb"):
        ts = [e["ts"] for e in rs if e["source"]["component"] == p]
        assert ts == sorted(ts)


def test_work_side_cannot_reach_the_core_mouths_but_can_reach_8081(world):
    t = world.net.transport("pa", zone="work")
    for url in ("http://timeline:8080/v1/events", "http://ledger:8080/v1/todos", "http://control:8080/v1/commands", "http://intake:8080/x"):
        with pytest.raises(Unavailable):
            t("GET", url, {}, None, 5)
    reach = [c.outcome for c in world.net.calls if c.caller == "pa"]
    assert len(reach) == 4 and set(reach) <= {"no_route", "refused"}
    from axcommon.client import ApiClient, ApiError

    work = ApiClient("http://control:8081", "tok-pa", transport=t)
    assert {p["id"] for p in work.get("/v1/plugins")["plugins"]} >= set(PLUGINS)
    assert work.get("/v1/plugins/pa/effective")["plugin"] == "pa"
    with pytest.raises(ApiError) as err:
        work.post("/v1/commands", {"target": "pb", "op": "set_mode", "args": {"mode": "draining"}})
    assert err.value.status == 404
    assert world.control.list_commands() == []


def test_faultnet_faults_are_scoped_and_lost_responses_still_reach_the_backend(fake_clock, fake_transport):
    from axcommon.client import ApiClient, ApiError
    from tests.fakes.fault import FaultNet

    fake_transport.add("POST", "/v1/events", json={"accepted": 1})
    net = FaultNet(fake_clock).add("timeline", fake_transport)
    a = ApiClient("http://timeline:8080", transport=net.transport("a"), timeout=2)
    b = ApiClient("http://timeline:8080", transport=net.transport("b"), timeout=2)
    net.stop("timeline", caller="a")
    with pytest.raises(Unavailable):
        a.post("/v1/events", {})
    assert b.post("/v1/events", {}) == {"accepted": 1}
    net.start("timeline", caller="a")
    net.delay("timeline", 3, times=1, caller="a")
    with pytest.raises(Unavailable):
        a.post("/v1/events", {})
    assert fake_clock.monotonic() == 2
    net.respond("timeline", 429, {"error": {"code": "rate_limited"}}, times=1, caller="a")
    with pytest.raises(ApiError) as err:
        a.post("/v1/events", {})
    assert err.value.code == "rate_limited"
    sent = len(fake_transport.calls)
    net.lose_response("timeline", times=1, caller="a")
    with pytest.raises(Unavailable):
        a.post("/v1/events", {})
    assert len(fake_transport.calls) == sent + 1
    assert a.post("/v1/events", {}) == {"accepted": 1}
    assert [c.outcome for c in net.calls] == ["down", "delivered", "delayed_timeout", "faked", "lost", "delivered"]
    net.clear()
    assert net.faults == []


def test_chaos_restores_async_and_sync_methods_and_other_rounds_continue(fake_clock):
    class Part:
        def tick(self):
            return "ok"

        async def cycle(self):
            await asyncio.sleep(0)
            return "ok"

    part = Part()
    chaos = Chaos(fake_clock)
    chaos.raise_on(part, "cycle", times=1)
    chaos.garbage(part, "tick", "garbage")
    assert part.tick() == "garbage"
    stats = run(run_rounds({"faulty": part.cycle, "healthy": Part().cycle}, n=3, timeout_s=1))
    assert stats["faulty"].rounds == 3 and stats["faulty"].failed == 1 and stats["faulty"].ok == 2
    assert stats["healthy"].ok == 3
    chaos.restore()
    assert part.tick() == run(part.cycle()) == "ok"
    assert "tick" not in part.__dict__ and "cycle" not in part.__dict__


def test_faultnet_scopes_faults_limits_hits_and_distinguishes_a_lost_response(fake_clock, fake_transport):
    from axcommon.client import ApiClient, ServerError
    from tests.fakes.fault import FaultNet

    fake_transport.add("POST", "/v1/events", json={"accepted": 1})
    net = FaultNet(fake_clock).add("timeline", fake_transport)
    pa = ApiClient("http://timeline:8080", transport=net.transport("pa"), timeout=2)
    pb = ApiClient("http://timeline:8080", transport=net.transport("pb"), timeout=2)
    fault = net.respond("timeline", 503, caller="pa", method="POST", path="/v1/events", times=1)
    assert pb.post("/v1/events", {}) == {"accepted": 1}
    with pytest.raises(ServerError):
        pa.post("/v1/events", {})
    assert pa.post("/v1/events", {}) == {"accepted": 1} and fault.hits == 1
    net.delay("timeline", 5, caller="pa", times=1)
    before = fake_clock.monotonic()
    with pytest.raises(Unavailable):
        pa.post("/v1/events", {})
    assert fake_clock.monotonic() - before == 2
    count = len(fake_transport.calls)
    net.lose_response("timeline", caller="pa", method="POST", times=1)
    with pytest.raises(Unavailable):
        pa.post("/v1/events", {})
    assert len(fake_transport.calls) == count + 1
    assert net.calls[-1].outcome == "lost"
    assert pb.post("/v1/events", {}) == {"accepted": 1}
    net.stop("timeline", caller="pa")
    with pytest.raises(Unavailable):
        pa.post("/v1/events", {})
    net.start("timeline", caller="pa")
    assert pa.post("/v1/events", {}) == {"accepted": 1}
    net.clear()
    assert net.faults == []


def test_chaos_and_run_rounds_preserve_other_loops_and_restore_methods(fake_clock):
    from tests.fakes.fault import RoundStats

    class Part:
        def __init__(self):
            self.calls = 0

        async def tick(self):
            self.calls += 1

    broken, hung, healthy = Part(), Part(), Part()
    chaos = Chaos(fake_clock)
    chaos.raise_on(broken, "tick", RuntimeError("fault"), times=1)
    chaos.hang(hung, "tick")
    stats = run(run_rounds({"broken": broken.tick, "hung": hung.tick, "healthy": healthy.tick}, n=3, timeout_s=0.01))
    assert isinstance(stats["healthy"], RoundStats)
    assert stats["healthy"].ok == healthy.calls == 3
    assert stats["broken"].failed == 1 and stats["broken"].ok == 2
    assert stats["hung"].timed_out == 3
    assert all(st.rounds == 3 for st in stats.values())
    chaos.restore()
    assert "tick" not in broken.__dict__ and "tick" not in hung.__dict__
    recovered = run(run_rounds({"broken": broken.tick, "hung": hung.tick}, timeout_s=0.01))
    assert all(st.ok == 1 for st in recovered.values())


def test_memory_connector_down_buffers_the_review_summary_and_retries_after_restart(world):
    from core.manager.actions import Executor

    m = world.manager()
    world.memory.fail = True
    event = world.emit("inspection.phase", "inspector", {"phase": "review_request", "plan": {"inspection_id": "round-1"}})
    world.gateway.script(reply(noop()))
    m.fetch()
    m.run_cycle("event")
    assert m.store.state_of(event["id"]) == "handled"
    assert world.events("manager.decision")[-1]["data"]["steps"]["7"] == "queued"
    assert len(m.exec.memory_outbox) == 1 and not world.memory.remembered
    # Reconstruct the sender against the same volume, then retry in the next cycle.
    m.exec = Executor(m.c, world.tmp / "mgr-ledger-outbox", host="ci")
    world.memory.fail = False
    m.run_cycle("tick")
    assert len(m.exec.memory_outbox) == 0 and len(world.memory.remembered) == 1
    m.run_cycle("tick")
    assert len(world.memory.remembered) == 1


def test_memory_connector_down_review_is_buffered_and_the_next_cycle_sends_it(world):
    from core.manager.review import Reviewer

    m = world.manager()
    event = world.emit("inspection.phase", "inspector", {"phase": "review_request", "plan": {"inspection_id": "inspection-memory"}})
    world.memory.fail = True
    result = Reviewer(m)._remember(event, "inspection-memory", {"usage_by_family": {}, "backup_activations": 0, "retire_candidates": []}, [])
    assert result == "queued" and len(m.exec.memory_outbox) == 1
    assert world.memory.remembered == []
    assert m.run_cycle("tick")["errors"] == []
    assert len(m.exec.memory_outbox) == 1
    world.memory.fail = False
    m.run_cycle("tick")
    assert len(m.exec.memory_outbox) == 0
    assert len(world.memory.remembered) == 1 and world.memory.remembered[0]["source"] == event["id"]
    m.run_cycle("tick")
    assert len(world.memory.remembered) == 1

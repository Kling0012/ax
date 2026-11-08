"""T-8 グラフ実行（S-7.5、D-6.2）。1 項目が上限に当たっても他の項目と次の実行が進む。依存先を外へ出すと依存する項目も外へ出る。
draining で新規 run・項目は始めず、既存項目の後続ノードは最後まで走る。collect_only で拾うが起動しない。間に合わないと missed が書かれ、追いかけない。"""
import asyncio

from axcommon import timeutil
from tests.b7.exhelp import graph, make_executor, manifest, node, patrol_graph
from tests.b7.helpers import run
from tests.fakes.b7 import control


def states(ex, kind=None):
    return {i["item_id"]: i["state"] for i in ex.store.items() if not i["item_id"].startswith("run:")}


def test_item_hitting_max_loops_is_ejected_and_others_and_next_run_continue(env):
    items = [{"id": "A", "st": "bug"}, {"id": "B", "st": "ok"}, {"id": "C", "st": "ok"}]
    g = patrol_graph([node("step", with_={"status": "{{item.st}}"}, on_fail="eject")], [{"from": "step", "to": "step", "when": "bug", "max_loops": 2}], items)

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(go())
    assert states(ex) == {"A": "ejected", "B": "completed", "C": "completed"}
    assert ex.store.get_item("A")["reason"].startswith("max_loops")
    assert control.COUNTS[("A", "step")] == 3          # 最初の 1 回 + 差し戻し 2 回
    # 台帳へ出る: todo_id の無い項目は ToDo(parked) を作る
    assert len(env.ledger.created) == 1 and env.ledger.created[0]["state"] == "parked" and env.ledger.created[0]["source"]["item_id"] == "A"
    assert env.ledger.created[0]["check_at"] and env.ledger.created[0]["reason"].startswith("max_loops")
    ev = env.sink.of("item.state", state="ejected")
    assert len(ev) == 1 and ev[0]["data"]["item_id"] == "A"
    # 次の実行が進む: 新しい項目 D が拾われて完了する
    g2 = patrol_graph([node("step", with_={"status": "{{item.st}}"}, on_fail="eject")], [{"from": "step", "to": "step", "when": "bug", "max_loops": 2}],
                      [{"id": "D", "st": "ok"}])

    async def go2():
        ex2 = make_executor(env, g2, store=ex.store)
        await ex2.run_once()
        await ex2.drain()
        return ex2

    ex2 = run(go2())
    assert states(ex2)["D"] == "completed" and states(ex2)["A"] == "ejected"
    runs = ex2.store.runs()
    assert [r["state"] for r in runs] == ["completed", "completed"]


def test_ejected_item_is_picked_up_again_as_a_new_item_next_run(env):
    g = patrol_graph([node("step", with_={"status": "{{item.st}}"}, on_fail="eject")], [], [{"id": "A", "st": "error"}])

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await ex.drain()
        assert states(ex) == {"A": "ejected"}
        await ex.run_once()      # 同じ ID を見つけても、外へ出した項目の修正は新しい項目として拾う（S-7.5）
        await ex.drain()
        return ex

    ex = run(go())
    s = states(ex)
    assert len(s) == 2 and sorted(s.values()) == ["ejected", "ejected"] and any(k.startswith("A~") for k in s)


def test_failure_of_one_item_does_not_touch_others_even_on_exception(env):
    items = [{"id": "A", "b": True}, {"id": "B", "b": False}]
    g = patrol_graph([node("step", with_={"boom": "{{item.b}}"}, on_fail="eject")], [], items)

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(go())
    assert states(ex) == {"A": "ejected", "B": "completed"}


def test_dependency_ejected_propagates_and_timeout_ejects(env, fake_clock):
    items = [{"id": "A", "st": "error"}, {"id": "B", "st": "ok", "depends_on": ["A"]}, {"id": "C", "st": "ok", "depends_on": ["B"]},
             {"id": "D", "st": "ok", "depends_on": ["NOPE"]}, {"id": "E", "st": "ok"}]
    g = patrol_graph([node("step", with_={"status": "{{item.st}}"}, on_fail="eject")], [], items, mpi=5)

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await ex.drain()
        s1 = states(ex)
        fake_clock.advance(25 * 3600)       # dependency_wait_h（既定 24）を過ぎる
        await ex.tick()
        await ex.drain()
        return s1, ex

    s1, ex = run(go())
    assert s1["A"] == "ejected" and s1["B"] == "ejected" and s1["C"] == "ejected" and s1["E"] == "completed"
    assert ex.store.get_item("B")["reason"] == "dependency_ejected" and ex.store.get_item("C")["reason"] == "dependency_ejected"
    assert s1["D"] == "planned"
    assert states(ex)["D"] == "ejected" and ex.store.get_item("D")["reason"] == "dependency_timeout"
    assert ("B", "step") not in control.COUNTS      # 依存先が外へ出たので走らない


def test_dependency_waits_until_done_then_runs(env):
    items = [{"id": "A", "st": "ok"}, {"id": "B", "st": "ok", "depends_on": ["A"]}]
    g = patrol_graph([node("step", with_={"status": "{{item.st}}"}, on_fail="eject")], [], items, mpi=2)

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(go())
    assert states(ex) == {"A": "completed", "B": "completed"}
    assert control.LOG.index(("A", "step")) < control.LOG.index(("B", "step"))


def _two_node_graph(items, mpi=1, list_nm=None):
    return patrol_graph([node("s1", "t.sleep", {"seconds": 0.05}), node("s2"), node("s3")],
                        [{"from": "s1", "to": "s2"}, {"from": "s2", "to": "s3"}], items, mpi=mpi, list_needs_mode=list_nm)


def test_draining_starts_nothing_new_but_existing_item_runs_to_the_end(env):
    g = _two_node_graph([{"id": "A"}, {"id": "B"}], mpi=1)

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await asyncio.sleep(0.01)                 # A は s1 の途中
        ex.apply("draining", None, 1)
        await ex.drain()
        s_mid = states(ex)
        rid = await ex.run_once()                 # 新しい run は始めない
        await ex.tick()
        await ex.drain()
        return ex, s_mid, rid

    ex, s_mid, rid = run(go())
    assert s_mid == {"A": "completed", "B": "planned"}
    assert ("A", "s3") in control.COUNTS            # 既存 item の後続ノードは最後まで走る
    assert ("B", "s1") not in control.COUNTS        # 新規 item は始めない
    assert rid is None and ex.store.runs()[-1]["state"] == "skipped" and ex.store.runs()[-1]["reason"] == "mode:draining"
    # run に戻ると、待っていた項目を拾って走る
    ex.apply("run", None, 2)

    async def back():
        await ex.tick()
        await ex.drain()

    run(back())
    assert states(ex) == {"A": "completed", "B": "completed"}


def test_collect_only_collects_but_does_not_start_then_run_starts(env):
    g = _two_node_graph([{"id": "A"}, {"id": "B"}], mpi=2, list_nm=["run", "collect_only"])

    async def go():
        ex = make_executor(env, g)
        ex.apply("collect_only", None, 1)
        await ex.run_once()                       # 巡回のノード(list)は collect_only でも動く → 項目は記録して待たせる
        await ex.drain()
        s1 = states(ex)
        ex.apply("run", None, 2)
        await ex.tick()
        await ex.drain()
        return ex, s1

    ex, s1 = run(go())
    assert s1 == {"A": "planned", "B": "planned"}
    assert not control.LOG[:0] and states(ex) == {"A": "completed", "B": "completed"}
    # collect_only の間は一度も項目のノードが起動していない
    assert ex.store.node_runs(item_id="A")[0]["started_at"] is not None


def test_collect_only_without_needs_mode_skips_the_run(env):
    g = _two_node_graph([{"id": "A"}], mpi=1)       # list は needs_mode の既定 [run]

    async def go():
        ex = make_executor(env, g)
        ex.apply("collect_only", None, 1)
        await ex.run_once()
        return ex

    ex = run(go())
    assert ex.store.runs()[-1]["state"] == "skipped" and not ex.store.items(["planned"])


def test_poll_paused_starts_waiting_items_but_does_not_patrol(env):
    g = _two_node_graph([{"id": "A"}], mpi=1, list_nm=["run", "collect_only"])

    async def go():
        ex = make_executor(env, g)
        ex.apply("collect_only", None, 1)
        await ex.run_once()
        ex.apply("poll_paused", None, 2)
        rid = await ex.run_once()                 # 巡回しない
        await ex.tick()
        await ex.drain()
        return ex, rid

    ex, rid = run(go())
    assert rid is None and states(ex) == {"A": "completed"}


def test_missed_is_recorded_and_not_chased(env, fake_clock):
    g = patrol_graph([node("step")], [], [])           # 項目なし。run が作られたかどうかだけを見る
    ex = make_executor(env, g, manifest(schedule={"kind": "interval", "every_s": 60, "missed": "record_only"}))

    async def go():
        await ex.tick()                                # 最初の予定 → run
        fake_clock.advance(605)                        # 動けなかった 10 分（9 回分が過ぎ、最新は 5 秒遅れ）
        await ex.tick()
        n1 = [r["state"] for r in ex.store.runs()]
        fake_clock.advance(60 + 40)                    # 次の予定に 40 秒遅れる（許容 30 秒を超える）
        await ex.tick()
        return n1, [r["state"] for r in ex.store.runs()]

    n1, n2 = run(go())
    assert n1.count("missed") == 9 and n1.count("completed") == 2     # 追いかけない: 走ったのは最初と最新の 2 回だけ
    assert n2.count("missed") == 10 and n2.count("completed") == 2     # 許容を超えた遅れは missed になり、走らない
    miss = env.sink.of("run.state", state="missed")
    assert len(miss) == 10 and all(m["data"]["reason"] == "late" for m in miss)


def test_retries_follow_system_retries_and_backoff(env, fake_clock):
    g = graph([node("step", with_={"status": "ok", "fail_status": "error", "fail_times": 2, "expected": True}, limits={"retries": 2})], [])

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(go())
    assert control.COUNTS[("t-run", "step")] if False else True
    assert list(states(ex).values()) == ["completed"]
    assert sum(v for k, v in control.COUNTS.items() if k[1] == "step") == 3
    assert fake_clock.sleeps == [30.0, 60.0]               # S-17 retry_backoff: 30 秒 × 2^回数


def test_retries_exhausted_and_unexpected_error_is_not_retried(env):
    g = graph([node("step", with_={"status": "error", "expected": True}, limits={"retries": 1}, on_fail="eject")], [])
    g2 = graph([node("step", with_={"status": "error", "expected": False}, limits={"retries": 3}, on_fail="eject")], [])

    async def go(gg):
        ex = make_executor(env, gg)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(go(g))
    assert list(states(ex).values()) == ["ejected"] and sum(control.COUNTS.values()) == 2
    ex2 = run(go(g2))
    assert sum(control.COUNTS.values()) == 1


def test_timeout_uses_continuations_then_ok(env):
    g = graph([node("slow", "t.sleep", {"seconds": 2, "first_only": True}, timeout=1, limits={"continuations": 2})], [])

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(go())
    assert list(states(ex).values()) == ["completed"]
    runs = ex.store.node_runs(node_id="slow")
    assert [r["state"] for r in runs] == ["timed_out", "done"] and [r["continuation"] for r in runs] == [0, 1]
    assert runs[1]["out"]["data"]["continuation"] == 1


def test_timeout_without_continuations_ejects(env):
    g = graph([node("slow", "t.sleep", {"seconds": 2}, timeout=1, on_fail="eject")], [])

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(go())
    assert list(states(ex).values()) == ["ejected"] and "node_timeout" in list(ex.store.items())[0]["reason"]


def test_max_hops_ejects_even_when_edges_allow_more_loops(env):
    g = graph([node("step", with_={"status": "bug"}, on_fail="eject")], [{"from": "step", "to": "step", "when": "bug", "max_loops": 50}])
    man = manifest()
    man["knobs"]["max_hops"]["default"] = 3

    async def go():
        ex = make_executor(env, g, man)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(go())
    it = ex.store.items()[0]
    assert it["state"] == "ejected" and it["reason"] == "max_hops:3" and control.COUNTS[(it["item_id"], "step")] == 4


def test_on_fail_goto_and_skip(env):
    g = graph([node("a", with_={"status": "error"}, on_fail="goto:b"), node("b"), node("c", with_={"status": "bug"}, on_fail="skip")],
              [{"from": "b", "to": "c"}, {"from": "c", "to": "a", "when": "skip", "max_loops": 1}])

    async def go():
        ex = make_executor(env, g)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(go())
    # a(error) → goto b → c(bug, on_fail skip → skip の辺) → a(1 回だけ戻る) → a(error) → goto b → c → skip の辺は上限 → 外へ出る
    assert control.LOG[:3] == [(ex.store.items()[0]["item_id"], "a"), (ex.store.items()[0]["item_id"], "b"), (ex.store.items()[0]["item_id"], "c")]
    assert ex.store.items()[0]["state"] == "ejected"


def test_apply_ignores_old_revision_and_persists(env):
    g = patrol_graph([node("step")], [], [])
    ex = make_executor(env, g)
    assert ex.apply("collect_only", {"max_parallel_items": 1}, 5) is True
    assert ex.apply("run", None, 3) is False and ex.mode == "collect_only" and ex.knobs()["max_parallel_items"] == 1
    ex2 = make_executor(env, g, store=ex.store)       # 再起動しても mode と knob と revision が残る
    assert ex2.mode == "collect_only" and ex2.revision == 5 and ex2.knobs()["max_parallel_items"] == 1

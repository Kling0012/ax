"""T-9 二重実行（D-6.2）。launching の後で部品を落として再起動すると、照合で決着し、外への操作が 2 回にならない。照合できないと lost になる。"""
import asyncio

import pytest

from runtime.subloop.nodeapi import ok
from runtime.subloop.store import Store
from tests.b7.exhelp import ROOT, graph, make_executor, manifest, node
from tests.b7.helpers import run
from tests.fakes.b7 import control
from tests.fakes.b7.parts import CmdResult


class Crash(BaseException):
    """プロセスが落ちた状態を作る（Exception ではないので、実行系は後始末をしない）。"""


def _effect_graph(key="k1", reconcilable=True):
    return graph([node("send", "t.effect", {"key": key, "reconcilable": reconcilable}, on_fail="eject")], [])


def _crash_run(env, g, store=None, after=False, before=False):
    async def go():
        ex = make_executor(env, g, store=store)
        control.CRASH_AFTER_OP["on"], control.CRASH_BEFORE_OP["on"] = after, before
        await ex.run_once()
        await ex.drain()
        return ex

    return run(go())


def _recover(env, g, store):
    saved = control.EXTERNAL[:]

    async def go():
        ex2 = make_executor(env, g, store=store)
        control.EXTERNAL[:] = saved
        rep = await ex2.recover()
        await ex2.drain()
        return ex2, rep

    return run(go())


def test_crash_after_the_operation_is_settled_by_reconcile_without_resending(env):
    g = _effect_graph()
    db = env.work_root.parent / "runtime.sqlite"
    ex = _crash_run(env, g, store=Store(db), after=True)
    assert control.EXTERNAL == ["k1"]
    assert [r["state"] for r in ex.store.pending_launching()] == ["launching"]     # launching が残った
    control.CRASH_AFTER_OP["on"] = False
    ex.store.close()
    ex2, rep = _recover(env, g, Store(db))
    assert control.EXTERNAL == ["k1"]                                              # 外への操作は 1 回のまま
    assert len(rep["resolved"]) == 1 and not rep["lost"] and not rep["retry"]
    item = ex2.store.items()[0]
    assert item["state"] == "completed"
    nr = ex2.store.node_runs(node_id="send")[0]
    assert nr["state"] == "done" and nr["out"]["data"]["reconciled"] is True
    assert env.sink.of("node.state", state="launching") and env.sink.of("node.state", state="reconciled")


def test_crash_before_the_operation_reconcile_says_not_done_and_reruns_once(env):
    g = _effect_graph()
    ex = _crash_run(env, g, before=True)
    assert control.EXTERNAL == []
    control.CRASH_BEFORE_OP["on"] = False
    ex2, rep = _recover(env, g, ex.store)
    assert control.EXTERNAL == ["k1"] and len(rep["retry"]) == 1
    assert ex2.store.items()[0]["state"] == "completed"
    assert [r["state"] for r in ex2.store.node_runs(node_id="send")] == ["abandoned", "done"]


def test_unreconcilable_launching_becomes_lost_and_is_not_resent(env):
    g = _effect_graph(reconcilable=False)
    ex = _crash_run(env, g, after=True)
    control.CRASH_AFTER_OP["on"] = False
    ex2, rep = _recover(env, g, ex.store)
    assert control.EXTERNAL == ["k1"]                         # 再送しない（禁止）
    assert len(rep["lost"]) == 1
    assert ex2.store.node_runs(node_id="send")[0]["state"] == "lost"
    it = ex2.store.items()[0]
    assert it["state"] == "ejected" and it["reason"].startswith("lost:")
    assert env.sink.of("node.state", state="lost")
    assert env.ledger.created and env.ledger.created[0]["state"] == "parked"          # 外へ出す


def test_interrupted_node_without_launching_is_simply_rerun(env):
    g = graph([node("a", "t.step"), node("b", "t.step")], [{"from": "a", "to": "b"}])
    control.reset()

    async def first():
        ex = make_executor(env, g)
        orig = ex.registry.get("t.step").run

        async def crashing(ctx, inp):
            if ctx.node_id == "b":
                raise Crash()
            return await orig(ctx, inp)

        ex.registry.get("t.step").run = crashing
        await ex.run_once()
        await asyncio.gather(*ex._tasks.values(), return_exceptions=True)
        return ex

    ex = run(first())
    assert ex.store.items()[0]["state"] == "running" and ex.store.unfinished_node_runs()[0]["node_id"] == "b"
    ex2, rep = _recover(env, g, ex.store)
    assert ex2.store.items()[0]["state"] == "completed"
    assert [r["state"] for r in ex2.store.node_runs(node_id="b")] == ["interrupted", "done"]
    assert [r["state"] for r in ex2.store.node_runs(node_id="a")] == ["done"]       # 済んだノードはやり直さない


@pytest.mark.parametrize("typ", ["comment", "pr_open", "merge", "label"])
def test_real_github_types_do_not_post_twice_after_a_crash(env, typ):
    gh = env.github
    gh.prs["o/r"] = {7: {"number": 7, "head": {"ref": "ai/x", "sha": "f" * 40}, "base": {"ref": "main"}, "merged": False, "html_url": "u"}}
    w = {"comment": {"repo": "o/r", "number": 7, "body": "こんにちは"},
         "pr_open": {"repo": "o/r", "head": "ai/new", "base": "main", "title": "t"},
         "merge": {"repo": "o/r", "number": 7, "sha": "f" * 40},
         "label": {"repo": "o/r", "number": 7, "add": ["ai-done"]}}[typ]
    meth = {"comment": "comment", "pr_open": "pr_open", "merge": "merge", "label": "add_labels"}[typ]
    orig = getattr(gh, meth)

    def crash_after(*a, **k):
        orig(*a, **k)
        raise Crash()

    setattr(gh, meth, crash_after)
    g = graph([node("n", f"gh.{typ}", w, on_fail="eject")], [])
    ex = _crash_run(env, g)
    assert len(gh.posted) == 1 and ex.store.pending_launching()
    setattr(gh, meth, orig)
    ex2, rep = _recover(env, g, ex.store)
    assert len(gh.posted) == 1                                  # 2 回目の操作は無い
    assert len(rep["resolved"]) == 1 and ex2.store.items()[0]["state"] == "completed"


def test_git_push_crash_after_push_is_reconciled_by_ls_remote(env):
    env.git.dirty[str(env.work_root / "job-wt-a")] = False
    (env.work_root / "job-wt-a").mkdir()
    orig = env.git.run

    async def crashing(args, cwd=None, timeout_s=None):
        r = await orig(args, cwd=cwd)
        if args[0] == "push":
            raise Crash()
        return r

    env.git.run = crashing
    g = graph([node("p", "git.push", {"remote": "work.git", "branch": "ai/x", "workdir_id": "wt-a"})], [])
    ex = _crash_run(env, g)
    assert len(env.git.pushes()) == 1 and ex.store.pending_launching()
    env.git.run = orig
    ex2, rep = _recover(env, g, ex.store)
    assert len(env.git.pushes()) == 1 and len(rep["resolved"]) == 1
    assert ex2.store.items()[0]["state"] == "completed"
    assert ex2.store.get_item(ex2.store.items()[0]["item_id"])["artifacts"]["branch"] == "ai/x"


def test_git_push_unreachable_remote_cannot_reconcile_so_lost(env):
    (env.work_root / "job-wt-a").mkdir()
    orig = env.git.run

    async def crashing(args, cwd=None, timeout_s=None):
        r = await orig(args, cwd=cwd)
        if args[0] == "push":
            raise Crash()
        return r

    env.git.run = crashing
    g = graph([node("p", "git.push", {"remote": "work.git", "branch": "ai/x", "workdir_id": "wt-a"})], [])
    ex = _crash_run(env, g)
    env.git.run = orig
    env.git.fail_on["ls-remote"] = CmdResult(128, "", "unreachable")
    ex2, rep = _recover(env, g, ex.store)
    assert len(env.git.pushes()) == 1 and len(rep["lost"]) == 1
    assert ex2.store.items()[0]["state"] == "ejected"

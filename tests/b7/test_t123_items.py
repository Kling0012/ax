"""T-123 項目の受け渡し（S-7.10）の、サブループ側。入口が制御 API の中継で `POST /items` に渡す。acceptance の例か境界が欠けた項目は拒否される。
同じ item_key は 1 回だけ受け付ける。inbox_max 超えは inbox_full。draining では保存だけして走らず、run に戻ると拾う。completed で item.state(todo_id)、
ejected でも item.state(todo_id)。サブループは ToDo の遷移を呼べない。（入口・制御 API の中継の資格検査は b3・b12 の試験。）"""
import asyncio

import pytest

from runtime.subloop.inbox import accept_item
from tests.b7.helpers import run
from tests.b7.test_t11_tdd import RED_FAIL, item, write_test


def mk(env, **kw):
    ex = env.work_tdd()
    env.pool.effects["test_design"] = write_test
    env.pool.script("verify", RED_FAIL)
    return ex


def test_acceptance_must_have_examples_and_boundaries(env):
    ex = mk(env)
    for acc in ({"examples": ["a"]}, {"boundaries": ["b"]}, {"examples": [], "boundaries": []}, None, {}):
        body = item()
        if acc is None:
            del body["acceptance"]
        else:
            body["acceptance"] = acc
        code, resp = accept_item(ex, body)
        assert code == 400 and resp["error"]["code"] == "acceptance_required"
    assert ex.store.inbox_queued() == 0 and not env.sink.of("item.state")


def test_malformed_item_is_rejected_with_422(env):
    ex = mk(env)
    assert accept_item(ex, {**item(), "role": "review"})[0] == 422
    assert accept_item(ex, {**item(), "kind": "other"})[0] == 422
    b = item()
    del b["item_key"]
    assert accept_item(ex, b)[0] == 422


def test_same_item_key_is_accepted_once(env):
    ex = mk(env)
    c1, r1 = accept_item(ex, item())
    c2, r2 = accept_item(ex, item(title="別の題でも同じキー"))
    assert (c1, r1["duplicate"], r1["state"]) == (200, False, "queued") and (c2, r2["duplicate"]) == (200, True)
    assert ex.store.inbox_queued() == 1 and len(env.sink.of("item.state", state="planned")) == 1
    planned = env.sink.of("item.state", state="planned")[0]["data"]
    assert planned["item_id"] == "todo-1" and planned["todo_id"] == "todo-1"            # item.state(planned, data.todo_id)

    async def go():
        await ex.run_once()
        await ex.drain()
        assert accept_item(ex, item())[1]["duplicate"] is True and accept_item(ex, item())[1]["state"] == "taken"     # 取られた後でも二重に受けない

    run(go())
    assert len(ex.store.items()) == 2 and len(env.pool.by_role("impl")) == 1          # 項目 todo-1 と巡回の疑似項目 run:…


def test_inbox_full_returns_429_and_saves_nothing(env):
    ex = mk(env)
    ex.apply("draining", {"inbox_max": 2}, 1)
    assert accept_item(ex, item(item_key="a", todo_id="a"))[0] == 200
    assert accept_item(ex, item(item_key="b", todo_id="b"))[0] == 200
    code, resp = accept_item(ex, item(item_key="c", todo_id="c"))
    assert code == 429 and resp["error"]["code"] == "inbox_full"
    assert ex.store.inbox_queued() == 2 and ex.store.inbox_state("c") is None
    # 入口は ToDo を parked（inbox_full、1 時間後）にする。サブループは何もしない
    assert not env.ledger.created and not env.sink.of("item.state", item_id="c")


@pytest.mark.parametrize("mode", ["draining", "collect_only", "poll_paused"])
def test_modes_other_than_run_only_save_and_run_picks_them_up_later(env, mode):
    ex = mk(env)
    ex.apply(mode, None, 1)

    async def go():
        assert accept_item(ex, item())[0] == 200
        await asyncio.sleep(0.01)
        await ex.tick()
        await ex.drain()
        assert ex.store.runs() == [] or all(r["state"] == "skipped" for r in ex.store.runs())     # run を作らない
        assert not env.pool.submitted and ex.store.inbox_queued() == 1
        ex.apply("run", None, 2)
        await ex.tick()                                                                           # run に戻ってから拾う
        await ex.drain()

    run(go())
    assert ex.store.get_item("todo-1")["state"] == "completed" and ex.store.inbox_queued() == 0


def test_in_run_mode_a_run_is_created_right_after_receipt(env):
    ex = mk(env)

    async def go():
        assert accept_item(ex, item())[0] == 200      # kick() が run を作る（tick を待たない）
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ex.store.get_item("todo-1") and ex.store.get_item("todo-1")["state"] == "completed":
                break
        await ex.drain()

    run(go())
    assert ex.store.get_item("todo-1")["state"] == "completed"
    assert [r["trigger_kind"] for r in ex.store.runs()] == ["items"]


def test_completed_item_writes_item_state_with_todo_id_and_branch_and_no_transition(env):
    ex = mk(env)

    async def go():
        accept_item(ex, item())
        await ex.run_once()
        await ex.drain()

    run(go())
    done = env.sink.of("item.state", state="completed")
    assert len(done) == 1 and done[0]["data"]["todo_id"] == "todo-1" and done[0]["data"]["artifacts"]["branch"] == "ai/todo-1"
    assert done[0]["data"]["artifacts"]["remote"] == "work.git"
    assert env.git.pushes() and env.git.pushes()[0][0][1] == "/git/work.git"                          # work.git へ push するだけ
    assert not env.ledger.transitions and not env.ledger.created                                       # ToDo の遷移は台帳が item.state を読んで行う


@pytest.mark.parametrize("kind,branch", [("subloop_create", "subloop/new-one"), ("core_change", "core/timeline-fix")])
def test_subloop_create_and_core_change_complete_with_their_branches(env, kind, branch):
    ex = mk(env)

    async def go():
        accept_item(ex, item(kind=kind, branch=branch, project="ax", template="work-tdd" if kind == "subloop_create" else None) if kind == "subloop_create" else item(kind=kind, branch=branch, project="ax"))
        await ex.run_once()
        await ex.drain()

    run(go())
    done = env.sink.of("item.state", state="completed")[0]["data"]
    assert done["artifacts"]["branch"] == branch and done["todo_id"] == "todo-1"
    assert env.git.calls and any(c[0][0] == "clone" and "/git/ax.git" in c[0] for c in env.git.calls)       # ax プロジェクトは AX_REPO_AX から


def test_ejected_item_writes_item_state_with_todo_id_and_never_creates_another_todo(env):
    ex = mk(env)
    env.pool.scripts["impl"] = [{"state": "failed", "verify": {"rc": 1, "log_tail": "x"}}] * 6

    async def go():
        accept_item(ex, item())
        await ex.run_once()
        await ex.drain()

    run(go())
    ev = env.sink.of("item.state", state="ejected")
    assert len(ev) == 1 and ev[0]["data"]["todo_id"] == "todo-1" and ev[0]["data"]["reason"]
    assert not env.ledger.created and not env.ledger.transitions


def test_queued_items_survive_a_restart(env, tmp_path):
    from runtime.subloop.store import Store

    db = tmp_path / "sl.db"
    s1 = Store(db)
    ex = env.work_tdd()
    ex.store = s1
    ex.apply("draining", None, 1)
    assert accept_item(ex, item())[0] == 200
    s1.close()
    s2 = Store(db)
    assert s2.inbox_queued() == 1 and s2.inbox_state("todo-1") == "queued"
    ex2 = env.executor(ex.manifest and __import__("runtime.subloop.graph", fromlist=["load_yaml"]).load_yaml(env.root / "plugins/subloop/work-tdd/graph.yaml"), ex.manifest, store=s2)
    assert ex2.mode == "draining"                    # mode も残る
    env.pool.effects["test_design"] = write_test
    env.pool.script("verify", RED_FAIL)

    async def go():
        ex2.apply("run", None, 2)
        await ex2.tick()
        await ex2.drain()

    run(go())
    assert s2.get_item("todo-1")["state"] == "completed"


def test_take_never_takes_more_than_free_slots(env):
    ex = mk(env)                                      # max_parallel_items 既定 1
    env.pool.scripts["verify"] = [RED_FAIL, {}, RED_FAIL, {}, RED_FAIL, {}]       # 項目ごとに gate.red（赤）→ verify.cmd（緑）
    for k in ("a", "b", "c"):
        accept_item(ex, item(item_key=k, todo_id=k))

    async def go():
        await ex.run_once()
        # take は 1 件だけ取る。残りは queued
        assert ex.store.inbox_queued() == 2
        await ex.drain()
        await ex.tick()
        await ex.drain()
        await ex.tick()
        await ex.drain()
        await ex.drain()

    run(go())
    assert ex.store.inbox_queued() == 0 and {i["item_id"]: i["state"] for i in ex.store.items() if not i["item_id"].startswith("run:")} == {"a": "completed", "b": "completed", "c": "completed"}


def test_http_layer_auth_routes_and_status(env):
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient

    from runtime.subloop.app import build_app

    ex = mk(env)
    ex.apply("draining", None, 1)                     # run のままだと受け取りの直後に run が走って inbox が空く
    app = build_app(ex, apply_token="tok", run_loops=False)
    body = item()
    with TestClient(app) as c:
        assert c.get("/healthz").json()["ok"] is True
        assert c.post("/items", json=body).status_code == 401                                       # control の資格だけ
        assert c.post("/items", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
        h = {"Authorization": "Bearer tok"}
        r = c.post("/items", json=body, headers=h)
        assert r.status_code == 200 and r.json()["accepted"] is True
        assert c.post("/items", json=body, headers=h).json()["duplicate"] is True
        bad = dict(body, item_key="z")
        bad["acceptance"] = {"examples": ["x"]}
        r = c.post("/items", json=bad, headers=h)
        assert r.status_code == 400 and r.json()["error"]["code"] == "acceptance_required"
        assert c.post("/apply", json={"mode": "draining", "revision": 3}).status_code == 401
        r = c.post("/apply", json={"mode": "draining", "revision": 3, "knobs": {"inbox_max": 1}}, headers=h)
        assert r.status_code == 200 and r.json()["applied"] is True
        assert c.post("/apply", json={"mode": "run", "revision": 2}, headers=h).json()["applied"] is False       # 古い revision は無視
        assert c.post("/apply", json={"mode": "bogus", "revision": 9}, headers=h).status_code == 422
        st = c.get("/status").json()
        assert st["mode"] == "draining" and st["knobs"]["inbox_max"] == 1 and set(st) >= {"mode", "knobs", "running", "queued", "last_run"}
        r = c.post("/items", json=dict(body, item_key="y", todo_id="y"), headers=h)
        assert r.status_code == 429 and r.json()["error"]["code"] == "inbox_full"
    # accepts_items でないサブループには /items が無い
    ex2 = mk(env)
    ex2.manifest = dict(ex2.manifest, accepts_items=False)
    with TestClient(build_app(ex2, apply_token="tok", run_loops=False)) as c:
        assert c.post("/items", json=body, headers={"Authorization": "Bearer tok"}).status_code in (404, 405)
    # 資格が渡されていないときは全拒否
    with TestClient(build_app(ex, apply_token=None, run_loops=False)) as c:
        assert c.post("/apply", json={"mode": "run", "revision": 99}, headers={"Authorization": "Bearer tok"}).status_code == 401


# B7-2 の補足試験。担当パス内で timer-runner と定義ファイルも検査する。
def make_timer(env, **kw):
    from runtime.timer_runner.runner import TimerRunner

    return TimerRunner(plugin_id="timer-runner", store=env.store, sink=env.sink,
                       handlers=kw.pop("handlers", {}), manifests=kw.pop("manifests", {}),
                       client_factory=kw.pop("client_factory", lambda pid, name: (pid, name)), **kw)


def timer_manifest(pid):
    return {"id": pid, "kind": "timer", "schedule": {"kind": "interval", "every_s": 60}}


def test_timer_periodic_jobs_do_not_wait_for_each_other_and_never_overlap(env, fake_clock):
    calls = []

    async def slow(ctx):
        calls.append(ctx.plugin_id)
        await release.wait()

    async def fast(ctx):
        calls.append(ctx.plugin_id)
        assert ctx.clients["ledger"] == ("b-fast", "ledger")

    tr = make_timer(env, handlers={"a-slow": slow, "b-fast": fast},
                    manifests={pid: timer_manifest(pid) for pid in ("a-slow", "b-fast")})

    async def go():
        nonlocal release
        release = asyncio.Event()
        await tr.tick()
        assert calls == ["a-slow", "b-fast"]
        fake_clock.advance(60)
        await tr.tick()
        assert calls.count("a-slow") == 1 and calls.count("b-fast") == 2
        assert env.sink.of("run.state", state="missed", reason="previous_run_active")
        release.set()
        await tr.drain()

    release = None
    run(go())
    assert not tr.status()["running"]


def test_timer_handler_failure_and_bad_schedule_leave_other_jobs_running(env):
    calls = []

    async def bad(ctx):
        raise RuntimeError("broken handler")

    async def good(ctx):
        calls.append(ctx.plugin_id)

    manifests = {pid: timer_manifest(pid) for pid in ("bad", "good", "invalid")}
    manifests["invalid"]["schedule"]["every_s"] = 0
    tr = make_timer(env, manifests=manifests, handlers={"bad": bad, "good": good})

    async def go():
        await tr.tick()
        await tr.drain()

    run(go())
    assert calls == ["good"]
    assert env.sink.of("run.state", state="failed", subject="bad")
    assert env.sink.of("run.state", state="completed", subject="good")


def test_timer_modes_record_missed_and_resume_without_catchup(env, fake_clock):
    calls = []

    async def handler(ctx):
        calls.append(ctx.planned_at)

    tr = make_timer(env, manifests={"job": timer_manifest("job")}, handlers={"job": handler})

    async def go():
        assert tr.apply("draining", 2)
        await tr.tick()
        await tr.drain()
        assert not calls
        fake_clock.advance(180)
        assert tr.apply("run", 3)
        await tr.tick()
        await tr.drain()
        assert len(calls) == 1
        assert len(env.sink.of("run.state", state="missed")) == 3

    run(go())


def test_timer_mode_and_revision_survive_restart_and_reject_old_apply(env, tmp_path):
    from runtime.subloop.store import Store

    path = tmp_path / "timer.db"
    s = Store(path)
    tr = make_timer(env)
    tr.store = s
    assert tr.apply("draining", 9)
    s.close()
    s2 = Store(path)
    try:
        from runtime.timer_runner.runner import TimerRunner
        tr2 = TimerRunner("timer-runner", s2, env.sink, {}, {}, lambda *a: None)
        assert tr2.mode == "draining" and tr2.revision == 9
        assert not tr2.apply("run", 8) and not tr2.apply("invalid", 10)
        assert not tr2.apply("run", True)
        assert tr2.mode == "draining"
    finally:
        s2.close()


def test_timer_effective_is_scoped_to_each_job_and_knobs_change_interval(env, fake_clock):
    calls = []

    async def handler(ctx):
        calls.append(ctx.plugin_id)

    tr = make_timer(env, handlers={pid: handler for pid in ("a", "b")},
                    manifests={pid: timer_manifest(pid) for pid in ("a", "b")}, require_effective=True)
    tr.set_effective("timer-runner", {"mode": "run", "knobs": {}, "revision": 1})
    tr.set_effective("a", {"mode": "draining", "knobs": {}, "revision": 2})
    tr.set_effective("b", {"mode": "run", "knobs": {"every_s": 120}, "revision": 2})
    tr.set_effective("a", {"mode": "run", "knobs": {}, "revision": 1})

    async def go():
        await tr.tick()
        await tr.drain()
        assert calls == ["b"]
        fake_clock.advance(60)
        await tr.tick()
        await tr.drain()
        assert calls == ["b"]

    run(go())
    assert tr.store.get_meta("effective:a")["mode"] == "draining"


def test_timer_without_initial_effective_does_not_start(env):
    calls = []

    async def handler(ctx):
        calls.append(ctx.plugin_id)

    tr = make_timer(env, handlers={"a": handler}, manifests={"a": timer_manifest("a")}, require_effective=True)

    async def go():
        await tr.tick()
        await tr.drain()

    run(go())
    assert not calls and env.sink.of("run.state", state="missed")


def test_timer_shutdown_cancels_only_running_jobs(env):
    async def slow(ctx):
        await asyncio.Event().wait()

    tr = make_timer(env, handlers={"job": slow}, manifests={"job": timer_manifest("job")})

    async def go():
        await tr.tick()
        assert tr.status()["running"] == ["job"]
        await tr.close()
        assert not tr.status()["running"]

    run(go())
    assert env.sink.of("run.state", state="cancelled", reason="shutdown")


def test_timer_events_use_the_jobs_own_sink_and_clients(env, fake_clock):
    from tests.fakes.b7.parts import FakeSink

    job_sink = FakeSink()

    async def handler(ctx):
        assert ctx.clients["resources"] == ("job", "resources")
        ctx.emit("handler.done", "job", {"ok": True})

    tr = make_timer(env, handlers={"job": handler}, manifests={"job": timer_manifest("job")},
                    sink_factory=lambda pid: job_sink if pid == "job" else env.sink)
    run(tr.run_job("job", fake_clock.now().isoformat()))
    assert job_sink.of("handler.done") and job_sink.of("run.state", state="completed")
    assert not env.sink.events


def test_timer_undefined_transport_never_uses_an_invented_api_or_stops_periodic_jobs(env, fake_clock):
    from runtime.timer_runner.ports import ControlTimerPort

    class NoHttp:
        def get(self, *a, **kw):
            raise AssertionError("undefined GET")

        post = get

    port = ControlTimerPort(NoHttp())
    with pytest.raises(NotImplementedError):
        port.list_timers()
    with pytest.raises(NotImplementedError):
        port.mark("t", "fired", None)
    with pytest.raises(NotImplementedError):
        run(port.fire({"id": "t", "action": "plugin.run_now"}))
    calls = []

    async def handler(ctx):
        calls.append(ctx.plugin_id)

    tr = make_timer(env, manifests={"job": timer_manifest("job")}, handlers={"job": handler}, timer_source=port)

    async def go():
        await tr.tick()
        await tr.drain()
        fake_clock.advance(60)
        await tr.tick()
        await tr.drain()

    run(go())
    assert calls == ["job", "job"] and tr._timers_undefined


def test_b7_definitions_and_template_match_common_schemas(repo_root):
    import yaml
    from axcommon import schemas
    from runtime.subloop.graphcheck import check_subloop_dir

    for p in ("plugins/subloop/work-tdd/manifest.yaml", "templates/subloop/manifest.yaml", "runtime/timer_runner/manifest.yaml"):
        schemas.validate("manifest", yaml.safe_load((repo_root / p).read_text()))
    for p in ("plugins/subloop/work-tdd", "templates/subloop"):
        errors = [f for f in check_subloop_dir(repo_root / p, root=repo_root) if f.severity == "error"]
        assert not errors, [str(f) for f in errors]
    for p in ("compose.d/b7.yaml", "compose.d/sl-work-tdd.yaml", "templates/subloop/compose.sl.yaml.tmpl"):
        doc = yaml.safe_load((repo_root / p).read_text())
        assert all("ports" not in service for service in doc["services"].values())
    registry = yaml.safe_load((repo_root / "plugins/registry.d/b7.yaml").read_text())
    assert all((repo_root / p["path"]).is_file() for p in registry["plugins"])
    schemas.validate("registry-fragment", registry)


def test_timer_entrypoint_uses_polling_and_rejects_ambiguous_apply(env, monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient
    from axcommon import client, creds
    from runtime.timer_runner.__main__ import main

    root = tmp_path / "timer-root"
    (root / "plugins").mkdir(parents=True)
    (root / "plugins" / "registry.yaml").write_text("plugins: []\n")
    monkeypatch.setenv("AX_HOME_IN_CONTAINER", str(root))
    monkeypatch.setenv("AX_DATA_DIR", str(tmp_path / "timer-data"))
    monkeypatch.setattr(creds, "read_secret_optional", lambda name: "fake-" + name)
    calls = []

    def get(self, path, **kw):
        calls.append(path)
        assert path == "/v1/plugins/timer-runner/effective"
        return {"mode": "draining", "knobs": {}, "revision": 4}

    monkeypatch.setattr(client.ApiClient, "get", get)

    def capture(app, **kw):
        with TestClient(app) as c:
            h = {"Authorization": "Bearer fake-control-apply-token"}
            assert c.get("/healthz").status_code == 200
            assert c.post("/apply", json={"mode": "run", "revision": 5}).status_code == 401
            r = c.post("/apply", json={"mode": "run", "revision": 5}, headers=h)
            assert r.status_code == 501 and r.json()["error"]["code"] == "timer_apply_target_undefined"
            assert c.get("/status").json()["mode"] == "draining"

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=capture))
    assert main() == 0
    assert calls == ["/v1/plugins/timer-runner/effective"]

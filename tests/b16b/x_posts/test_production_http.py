from __future__ import annotations

import time

import pytest

from starlette.testclient import TestClient

from runtime.subloop.__main__ import XInventoryBinding
from runtime.subloop.app import build_app
from runtime.subloop.graph import load_yaml
from tests.fakes.b7 import make_env


def test_http_inventory_and_unavailable_sender_are_fail_closed(tmp_path, repo_root, rig):
    env = make_env(tmp_path, repo_root)
    env.svc.plugin_id = "x-posts"
    env.control.eff = {"mode": "collect_only", "knobs": {}, "revision": 0}
    env.svc.x_posts_binding = XInventoryBinding()
    env.control.get = lambda path: {"delivery_targets": [rig.target.delivery_target()]}
    plugin = repo_root / "plugins/timer/x-posts"
    manifest = load_yaml(plugin / "manifest.yaml")
    ex = env.executor(load_yaml(plugin / "graph.yaml"), manifest)
    app = build_app(ex, apply_token="fake-token", tick_s=0.01)

    def wait_for(condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        assert condition()

    with TestClient(app) as client:
        assert client.get("/status").json()["mode"] == "collect_only"
        headers = {"authorization": "Bearer fake-token"}
        response = client.post("/apply", headers=headers, json={"mode": "collect_only", "revision": 2, "run_now": True})
        assert response.status_code == 200
        wait_for(lambda: bool(env.sink.of("automation.targets")))
        assert env.sink.of("automation.targets")[-1]["data"]["targets"] == [rig.target.delivery_target()]
        assert not env.ledger.created and not rig.env.posts
        for revision in (3, 4):
            before = len(env.sink.of("automation.targets"))
            response = client.post("/apply", headers=headers, json={"mode": "run", "revision": revision, "run_now": True})
            assert response.status_code == 200
            wait_for(lambda: len(env.sink.of("automation.targets")) > before)
        assert len(env.ledger.created) == 1
        assert env.ledger.created[0]["id"] == "x-posts-sender-unavailable"
        assert not rig.env.posts


@pytest.mark.parametrize("unknown", [False, True])
def test_http_fake_sender_requires_start_and_deduplicates(tmp_path, repo_root, rig, unknown):
    from x_posts import EnvResult
    from x_posts.runtime import RuntimeBinding

    env = make_env(tmp_path, repo_root)
    env.svc.plugin_id = "x-posts"
    env.control.eff = {"mode": "collect_only", "knobs": {}, "revision": 0}
    env.svc.x_posts_binding = RuntimeBinding(rig.engine, accounts={rig.target.destination: rig.target.account})
    plugin = repo_root / "plugins/timer/x-posts"
    ex = env.executor(load_yaml(plugin / "graph.yaml"), load_yaml(plugin / "manifest.yaml"))
    app = build_app(ex, apply_token="fake-token", tick_s=0.01)

    def wait_for(condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        assert condition()

    with TestClient(app) as client:
        headers = {"authorization": "Bearer fake-token"}
        assert client.get("/status").json()["mode"] == "collect_only"
        client.post("/apply", headers=headers, json={"mode": "collect_only", "revision": 2, "run_now": True})
        wait_for(lambda: bool(rig.events.records))
        assert not rig.env.posts
        completed = len(env.sink.of("run.state", state="completed"))
        client.post("/apply", headers=headers, json={"mode": "run", "revision": 3, "run_now": True})
        wait_for(lambda: len(env.sink.of("run.state", state="completed")) > completed)
        assert not rig.env.posts
        rig.start()
        rig.drafts("test proposal")
        assert not rig.env.posts
        if unknown:
            rig.env.responses = [EnvResult("timed_out")]
        client.post("/apply", headers=headers, json={"mode": "run", "revision": 4, "run_now": True})
        wait_for(lambda: len(rig.env.posts) == 1)
        wait_for(lambda: bool(env.sink.of("run.state", state="completed")))
        reopened = rig.reopen()
        if unknown:
            wait_for(lambda: rig.engine.store.candidates(rig.target)[0]["status"] == "unknown")
            assert reopened.recover()
            assert rig.lost and rig.lost[-1]["status"] == "lost"
        env.svc.x_posts_binding = RuntimeBinding(reopened, accounts={rig.target.destination: rig.target.account})
        before = len(env.sink.of("run.state", state="completed"))
        client.post("/apply", headers=headers, json={"mode": "run", "revision": 5, "run_now": True})
        wait_for(lambda: len(env.sink.of("run.state", state="completed")) > before)
        assert len(rig.env.posts) == 1


@pytest.mark.parametrize("case", ["unlisted", "future", "rejected", "permission"])
def test_http_queue_blocks_ineligible_targets_and_drafts(tmp_path, repo_root, rig, case):
    from axcommon import timeutil
    from x_posts import Draft, Inspection
    from x_posts.runtime import RuntimeBinding

    permission = rig.start()
    due = timeutil.add_seconds(timeutil.now_ts(), 600) if case == "future" else timeutil.now_ts()
    rig.worker.proposals = [Draft("candidate", "test proposal", due)]
    assert rig.engine.collect(rig.target, "test-day", scheduled_at=timeutil.now_ts())["result"] == "drafted"
    candidate = rig.engine.store.candidates(rig.target)[0]
    if case == "unlisted":
        rig.registry.targets.clear()
    elif case == "rejected":
        rig.inspector.by_text[("send", "test proposal")] = Inspection("rejected", "passed", "passed", "passed")
    elif case == "permission":
        rig.service.disable_permission(permission["id"], by="user")

    env = make_env(tmp_path, repo_root)
    env.svc.plugin_id = "x-posts"
    env.control.eff = {"mode": "collect_only", "knobs": {}, "revision": 0}
    env.svc.x_posts_binding = RuntimeBinding(rig.engine, accounts={rig.target.destination: rig.target.account})
    plugin = repo_root / "plugins/timer/x-posts"
    ex = env.executor(load_yaml(plugin / "graph.yaml"), load_yaml(plugin / "manifest.yaml"))
    app = build_app(ex, apply_token="fake-token", tick_s=0.01)

    with TestClient(app) as client:
        headers = {"authorization": "Bearer fake-token"}
        deadline = time.monotonic() + 3
        while not env.sink.of("run.state", state="completed") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert env.sink.of("run.state", state="completed")
        before = len(env.sink.of("run.state", state="completed"))
        response = client.post("/apply", headers=headers, json={"mode": "run", "revision": 10, "run_now": True})
        assert response.status_code == 200 and response.json()["applied"]
        deadline = time.monotonic() + 3
        while len(env.sink.of("run.state", state="completed")) <= before and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(env.sink.of("run.state", state="completed")) > before
        assert any(kind == "automation.targets" for kind, _, _ in rig.events.records)
        if case != "unlisted":
            assert any(event.get("node") == "send" and event["data"]["state"] == "ok"
                       for event in env.sink.of("node.state"))
        if case == "rejected":
            assert rig.engine.store.get(candidate["id"])["status"] == "rejected"
        assert rig.engine.journal.get("x-posts", rig.target.destination, candidate["id"]) is None
        assert rig.env.posts == []

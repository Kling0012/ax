"""Sending subloops wait for control on every process start."""
import pytest
from starlette.testclient import TestClient

from runtime.subloop.app import build_app
from tests.b7.exhelp import graph, make_executor, manifest, node
from tests.fakes.b7 import make_env


@pytest.mark.parametrize("saved_mode", [None, "run"])
def test_sending_subloop_waits_for_control_after_restart(tmp_path, repo_root, fake_clock, saved_mode):
    env = make_env(tmp_path, repo_root, fake_clock)
    if saved_mode:
        env.store.set_meta("mode", saved_mode)
    env.store.set_meta("revision", 4)
    definition = graph([node("start")], [])
    executor = make_executor(env, definition, manifest(outputs=["x.post"]))
    app = build_app(executor, apply_token="test-token", run_loops=False)
    with TestClient(app) as client:
        assert client.get("/status").json()["mode"] == "collect_only"
        assert client.post("/apply", json={"mode": "run", "revision": 4}).status_code == 401
        assert client.get("/status").json()["mode"] == "collect_only"
        response = client.post("/apply", headers={"Authorization": "Bearer test-token"},
                               json={"mode": "run", "revision": 4})
        assert response.status_code == 200
        assert client.get("/status").json()["mode"] == "run"

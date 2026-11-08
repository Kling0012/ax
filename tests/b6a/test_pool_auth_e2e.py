"""Authentication on both public worker-pool interfaces."""
import importlib

import pytest
from starlette.testclient import TestClient
from axcommon.creds import sha256_hex


@pytest.mark.parametrize("side,identity", [(0, "hq"), (0, "manager"), (0, "intake"), (0, "observer"), (0, "control"),
                                           (1, "change"), (1, "release"), (1, "work-tdd")])
def test_pool_authentication(tmp_path, monkeypatch, side, identity):
    core = tmp_path / "core"
    plugins = tmp_path / "plugins"
    core.write_text("".join(f"{name} {sha256_hex('test-' + name)}\n" for name in ("hq", "manager", "intake", "observer", "control", "gate")))
    plugins.write_text("".join(f"{name} {sha256_hex('test-' + name)}\n" for name in ("change", "release", "work-tdd", "timer-runner")))
    monkeypatch.setenv("AX_CORE_TOKENS", str(core))
    monkeypatch.setenv("AX_PLUGIN_TOKENS", str(plugins))
    pool = importlib.import_module("plugins.resource.worker-pool.app")
    apps = pool.build(pool.Config(db=str(tmp_path / "jobs.sqlite")), run_heartbeat_loop=False)
    with TestClient(apps[side]) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/jobs").status_code in (401, 403)
        for token in ("bad", "test-gate", "test-timer-runner", "test-change" if side == 0 else "test-hq"):
            assert client.get("/v1/jobs", headers={"Authorization": "Bearer " + token}).status_code in (401, 403)
        headers = {"Authorization": "Bearer test-" + identity}
        assert client.get("/v1/jobs", headers=headers).status_code == 200
        assert client.post("/v1/jobs", headers=headers, json={"role": "research", "prompt": "test", "sensitivity": "public"}).status_code == 202
        monkeypatch.setenv("AX_CORE_TOKENS" if side == 0 else "AX_PLUGIN_TOKENS", str(tmp_path / "missing"))
        assert client.get("/v1/jobs", headers=headers).status_code in (401, 403)
        assert client.get("/healthz").status_code == 200
    apps[2].store.close()


@pytest.mark.parametrize("side", [0, 1])
def test_starts_without_token_table(tmp_path, monkeypatch, side):
    monkeypatch.setenv("AX_CORE_TOKENS", str(tmp_path / "missing-core"))
    monkeypatch.setenv("AX_PLUGIN_TOKENS", str(tmp_path / "missing-plugins"))
    pool = importlib.import_module("plugins.resource.worker-pool.app")
    apps = pool.build(pool.Config(db=str(tmp_path / "jobs.sqlite")), run_heartbeat_loop=False)
    with TestClient(apps[side]) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/jobs", headers={"Authorization": "Bearer test-hq"}).status_code in (401, 403)
    apps[2].store.close()

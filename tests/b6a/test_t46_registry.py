"""T-46 資源台帳と週次確認（S-8.2）。

移行対象全件の ID・段階・状態・成績・出所を参照でき、停止中は停止のまま。
提供一覧の失敗は失敗と表示し、週次結果と前回差を残すが昇格に使わない。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.resources import discovery as disc
from tests.b6a.fixtures import resources_store, seed_models
from tests.fakes.b6a.model_gateway import FakeDiscovery


def test_migrated_all_fields_referable(tmp_path, fake_clock):
    store = resources_store(tmp_path)
    rows = seed_models(store)
    m = store.get("zcode/GLM-5.3")
    assert m["id"] and m["level"] and m["status"] == "active"
    assert m.get("pricing") and m.get("description") is not None


def test_stopped_stays_stopped(tmp_path):
    store = resources_store(tmp_path)
    # 停止中のモデルは有効化せず、旧実行経路に接続しない（S-8.2）
    seed_models(store, rows=[{"id": "old/stopped", "kind": "model", "family": "old", "tier": "free", "billing": "flat",
                              "level": "L1", "status": "retired", "prefix_cache": "unknown"}])
    # resolved migration? use store direct: create a retired row via seed on empty store with only retired
    # Since seed only on empty, the single row above alone is retired
    m = store.get("old/stopped")
    assert m["status"] == "retired"


def test_discovery_failure_is_failure_not_empty_success(tmp_path, fake_clock):
    store = resources_store(tmp_path)
    seed_models(store)
    fake = FakeDiscovery().fail_http(503)
    doc = disc.reconcile(store, transport=fake.transport, token="test-token", base_url="http://gateway.test")
    assert doc["ok"] is False and doc["error"]
    assert doc["provided"] == []  # 取得失敗を空の成功一覧としない
    got = store.last_discovery()
    assert got is not None and got["ok"] is False


def test_discovery_diffs_new_matched_unseen(tmp_path, fake_clock):
    store = resources_store(tmp_path)
    seed_models(store)
    fake = FakeDiscovery(provided=["zcode/GLM-5.3", "new/model-1"])
    doc = disc.reconcile(store, transport=fake.transport, token="test-token", base_url="http://gateway.test")
    assert "new/model-1" in doc["new_ids"]
    assert "zcode/GLM-5.3" in doc["matched"]
    assert "our-free-model/a-free" in doc["unseen"]
    assert doc["ok"] is True


def test_weekly_check_keeps_diff_but_not_used_for_promotion(tmp_path, fake_clock):
    store = resources_store(tmp_path)
    seed_models(store)
    r1 = store.record_weekly_check("zcode/GLM-5.3", reachable=True, responded=True, tool_ok=True, now="2026-11-05T12:00:00.000Z")
    assert r1["reachable"] is True
    r2 = store.record_weekly_check("zcode/GLM-5.3", reachable=False, responded=False, tool_ok=False,
                                   diff={"reachable": [True, False]}, error="down", now="2026-11-12T12:00:00.000Z")
    assert r2["reachable"] is False
    checks = store.weekly_checks(model_id="zcode/GLM-5.3")
    assert len(checks) == 2
    # 週次結果を昇格に使わない（S-8.5）
    from core.resources import promotion
    ev = promotion.evaluate(store, "zcode/GLM-5.3", now="2026-11-12T12:00:00.000Z")
    assert ev["new_level"] is None  # No capability trials yet, weekly checks don't cause promotion


def test_discovery_key_reaches_gateway_and_result_is_public(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from core.resources.app import Config, build

    (tmp_path / "model-gateway-key").write_text("test-token\n")
    monkeypatch.setenv("AX_SECRETS_PATH", str(tmp_path))
    store = resources_store(tmp_path)
    seen = []

    def gateway(method, url, headers, body, timeout):
        seen.append((method, url, headers.get("Authorization")))
        return 200, {"Content-Type": "application/json"}, b'{"data":[{"id":"vendor/model"}]}'

    disc.reconcile(store, transport=gateway, base_url="http://gateway.test")
    app, _, _ = build(Config(db=str(tmp_path / "resources.sqlite")), store=store, run_heartbeat_loop=False)
    with TestClient(app) as client:
        result = client.get("/v1/models/discovery")
        assert result.status_code == 200
        assert result.json()["ok"] is True
        assert result.json()["provided"] == ["vendor/model"]
    assert seen == [("GET", "http://gateway.test/v1/models", "Bearer test-token")]


@pytest.mark.parametrize("unreadable", [False, True])
def test_missing_key_records_failure_and_service_remains_available(tmp_path, monkeypatch, unreadable):
    from starlette.testclient import TestClient
    from core.resources.app import Config, build

    monkeypatch.setenv("AX_SECRETS_PATH", str(tmp_path))
    if unreadable:
        (tmp_path / "model-gateway-key").mkdir()
    store = resources_store(tmp_path)
    def gateway(*args):
        pytest.fail("gateway must not be called without a key")
    disc.reconcile(store, transport=gateway, base_url="http://gateway.test")
    app, _, _ = build(Config(db=str(tmp_path / "resources.sqlite")), store=store, run_heartbeat_loop=False)
    with TestClient(app) as client:
        result = client.get("/v1/models/discovery")
        assert result.status_code == 200
        assert result.json()["ok"] is False
        assert result.json()["error"] == "model_gateway_key_unavailable"
        assert client.get("/healthz").status_code == 200


def test_startup_discovery_preserves_previous_models_after_gateway_failure(tmp_path, monkeypatch):
    import time
    import axcommon.client
    from starlette.testclient import TestClient
    from core.resources.app import Config, build

    (tmp_path / "model-gateway-key").write_text("test-token\n")
    monkeypatch.setenv("AX_SECRETS_PATH", str(tmp_path))
    requests = []
    state = {"failed": False}

    def fake_gateway(method, url, headers, body, timeout):
        requests.append((method, url, headers.get("Authorization")))
        if state["failed"]:
            return 503, {}, b""
        return 200, {}, b'{"data":[{"id":"vendor/model"}]}'

    monkeypatch.setattr(axcommon.client, "urllib_transport", fake_gateway)
    store = resources_store(tmp_path)
    cfg = Config(db=str(tmp_path / "resources.sqlite"), model_gateway="http://gateway.test")

    def await_discovery(client, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get("/v1/models/discovery").json()
            if result.get("error") == expected:
                return result
            time.sleep(0.01)
        pytest.fail("discovery did not finish")

    app, _, _ = build(cfg, store=store, run_heartbeat_loop=False)
    with TestClient(app) as client:
        first = await_discovery(client, "")
        assert first["ok"] is True
        assert first["provided"] == ["vendor/model"]
    state["failed"] = True
    app, _, _ = build(cfg, store=store, run_heartbeat_loop=False)
    with TestClient(app) as client:
        second = await_discovery(client, "unavailable:ServerError")
        assert second["ok"] is False
        assert second["provided"] == first["provided"]
        assert client.get("/healthz").status_code == 200
    assert requests == [("GET", "http://gateway.test/v1/models", "Bearer test-token")] * 2

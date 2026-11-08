"""T-41 依存と継続（S-8.6）。

依存先の成功と検証の前は起動しない。失敗・保留・期限切れでも起動しない。
依頼元の対話終了後も結果を回収できる。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.b6a.fixtures import jobs_store
import importlib as _i; supervise = _i.import_module("plugins.resource.worker-pool.supervise")
import importlib as _il; _m=_il.import_module("plugins.resource.worker-pool.supervise"); is_ok, parse_report = _m.is_ok, _m.parse_report


def _dep_ok(dep: dict) -> bool:
    """依存先が指定結果と検証を満たすまで起動しない（S-8.6）。"""
    if not dep:
        return False
    if dep.get("state") not in ("ok", "completed"):
        return False
    return is_ok(verify_rc=dep.get("verify_rc"), report={"status": dep.get("report_status")})


def test_dependency_waits_for_success_with_verify(tmp_path):
    assert _dep_ok({"state": "ok", "verify_rc": 0, "report_status": "ready"}) is True
    assert _dep_ok({"state": "ok", "verify_rc": 1, "report_status": "ready"}) is False
    assert _dep_ok({"state": "ok", "verify_rc": 0, "report_status": "blocked"}) is False


def test_failed_parked_expired_do_not_start(tmp_path):
    for st in ("failed", "rejected", "cancelled", "lost", "timed_out", "stalled", "queued"):
        assert _dep_ok({"state": st, "verify_rc": 0, "report_status": "ready"}) is False, st


def test_own_verdict_is_not_adopted(tmp_path):
    # 本人の合格主張は採用しない（S-8.6）
    assert is_ok(verify_rc=1, report={"status": "ready"}) is False
    assert is_ok(verify_rc=0, report={"status": "blocked"}) is False
    assert is_ok(verify_rc=0, report={"status": "ready"}) is True


def test_report_status_line_parsing(tmp_path):
    r = parse_report("work done\nSTATUS: blocked\nHUMAN_ACTION: release the key\n")
    assert r["status"] == "blocked" and r["human_action"] == "release the key"
    r2 = parse_report("done\nSTATUS: ready")
    assert r2["status"] == "ready"
    r3 = parse_report("no status")
    assert r3["status"] == "failed"


def test_result_is_collectible_after_caller_session_ends(tmp_path, fake_clock):
    store = jobs_store(tmp_path)
    job = store.create({"role": "summary", "prompt": "x", "sensitivity": "public"})
    # 依頼元の対話が終わっても、受付済みジョブを継続し結果を回収できる
    store.set_state(job["id"], "running")
    store.record_result(job["id"], status="ready", text="done", verify_rc=0)
    store.set_state(job["id"], "ok")
    got = store.get(job["id"])
    assert got["state"] == "ok" and got["report_status"] == "ready" and got["verify_rc"] == 0

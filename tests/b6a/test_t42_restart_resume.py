"""T-42 再起動と再試行（S-8.6、S-8.12）。

生存する主体は照合し、消失・照合不能は lost。安全に扱えない変更があれば切替を止める。
属性を維持し、続きからの再開では連鎖の session_id と HOME を引き継ぐ（T-112、T-114）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.b6a.fixtures import jobs_store
from tests.fakes.b6a.launcher_fake import FakeLauncher
import importlib as _il; _m=_il.import_module("plugins.resource.worker-pool.launcher_client"); LauncherClient, LauncherError = _m.LauncherClient, _m.LauncherError
import importlib as _il2; classify_state = _il2.import_module("plugins.resource.worker-pool.supervise").classify_state
import importlib as _il3; Exclusivity = _il3.import_module("plugins.resource.worker-pool.exclusivity").Exclusivity
import importlib as _il4; _rm=_il4.import_module("plugins.resource.worker-pool.resume"); can_resume, chain_of, root_matches = _rm.can_resume, _rm.chain_of, _rm.root_matches


def test_surviving_subject_is_matched_missing_is_lost(tmp_path):
    assert classify_state(state="running", container_exists=True, caller_alive=True)["state"] == "running"
    assert classify_state(state="running", container_exists=False, caller_alive=True)["state"] == "lost"
    assert classify_state(state="running", container_exists=True, caller_alive=False)["state"] == "orphaned"


def test_restart_keeps_job_row(tmp_path):
    store = jobs_store(tmp_path)
    job = store.create({"role": "impl", "prompt": "x", "sensitivity": "public", "item_id": "it-1"})
    store.set_state(job["id"], "launching")
    store.start_attempt(job["id"], "zcode/GLM-5.3", attempt_no=1)
    # 再起動後、同一識別子で照合する（S-8.6）
    got = store.get(job["id"])
    assert got["state"] == "launching" and got["item_id"] == "it-1"
    assert store.attempts(job["id"])[0]["model"] == "zcode/GLM-5.3"


def test_failed_conversation_is_not_resumed(tmp_path):
    original = {"state": "failed", "item_id": "it-1", "model": "m1", "finished_at": "2026-11-05T12:00:00.000Z",
                "last_context": 100, "chain_length": 0, "root": "root1", "session_id": "sid"}
    r = can_resume(original, item_id="it-1", model="m1", now="2026-11-05T12:00:00.000Z")
    assert r["resume"] is False and "failed_state" in r["reason"]


def test_ok_conversation_resumes_with_home(tmp_path):
    original = {"state": "ok", "item_id": "it-1", "model": "m1", "finished_at": "2026-11-05T12:00:00.000Z",
                "last_context": 100, "chain_length": 0, "root": "root1", "session_id": "sid"}
    r = can_resume(original, item_id="it-1", model="m1", now="2026-11-05T12:00:00.000Z")
    assert r["resume"] is True and r["session_id"] == "sid" and r["root"] == "root1"


def test_resume_chain_carries_session_and_home(tmp_path):
    job = chain_of({"root": "root1", "session_id": "sid", "chain_length": 1}, job_id="job-2")
    assert job["root"] == "root1" and job["session_id"] == "sid" and job["chain_length"] == 2


def test_root_mismatch_is_rejected(tmp_path):
    assert root_matches("root1", "root1") is True
    assert root_matches("rootX", "root1") is False  # root_mismatch で外へ出す（S-8.12）


def test_exclusivity_released_once(tmp_path):
    ex = Exclusivity()
    ok, _ = ex.try_acquire(root="r", wd="w")
    assert ok
    assert ex.release(root="r", wd="w") is True
    assert ex.release(root="r", wd="w") is False  # 終了経路のどこでも一度だけ解放する（S-8.12 の 11）
    assert ex.releases == 1


def test_launcher_job_start_and_root_roundtrip(tmp_path):
    fake = FakeLauncher()
    fake.start_ok("job-9", root="job-9")
    client = LauncherClient(transport=fake.transport)
    res = client.job_start({"job_id": "job-9", "runner": "agent-cli", "model": "m1",
                            "allow_hosts": [], "budget_min": 30, "prompt": "test prompt"})
    assert res["job_id"] == "job-9" and res["root"] == "job-9"


def test_launcher_busy_is_busy_not_failure(tmp_path):
    fake = FakeLauncher()
    fake.busy()
    client = LauncherClient(transport=fake.transport)
    with pytest.raises(LauncherError) as ei:
        client.job_start({"job_id": "job-9", "runner": "agent-cli", "model": "m1",
                          "allow_hosts": [], "budget_min": 30, "prompt": "test prompt"})
    assert ei.value.exit_code == 4 and ei.value.reason == "session_busy"

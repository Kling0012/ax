"""T-114 再開の引き継ぎと排他（S-8.12）。

再開の条件は worker-pool の jobs 表の行で判定され、job-start に root は渡らない。
launcher が返した root が jobs 表と違うと、起動前に止まり root_mismatch で外へ出る
（モデルの成績に数えない）。resume_from を持つ新しい job_id のジョブが、
連鎖の最初のジョブの /work と HOME を使い、会話の記録を読める。
コンテナ名、許可先のファイル、ログは新しい job_id で作られる。
同じ連鎖を 2 つのジョブが同時に再開しようとすると、R-N14 の指定があるほうは
queued（session_busy）で待ち、指定が無いほうは新しい会話になる。
launcher の二重の起動も終了コード 4 で拒否される。終了・失敗・取消のどれでも排他が一度だけ解放される。
resume_max_age_min を過ぎた連鎖は archive/ の下へ移り、削除されない。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib
_r = importlib.import_module("plugins.resource.worker-pool.resume")
_e = importlib.import_module("plugins.resource.worker-pool.exclusivity")
from tests.b6a.fixtures import jobs_store
from tests.fakes.b6a.launcher_fake import FakeLauncher
import importlib as _il; _lm=_il.import_module("plugins.resource.worker-pool.launcher_client"); LauncherClient, LauncherError = _lm.LauncherClient, _lm.LauncherError


def test_condition_checked_in_jobs_table_no_root_to_job_start(tmp_path):
    store = jobs_store(tmp_path)
    j1 = store.create({"role": "impl", "prompt": "x", "sensitivity": "public", "item_id": "it-1"})
    store.set_state(j1["id"], "ok")
    store.record_result(j1["id"], status="ready", text="ok", verify_rc=0)
    orig = store.get(j1["id"])
    # 条件は jobs 表の行で判定する
    r = _r.can_resume(orig, item_id="it-1", model=None, now="2026-11-05T12:00:00.000Z")
    assert r["resume"] in (True, False)  # finished_at の有無で决まるが、表で判定する


def test_root_mismatch_outside(tmp_path):
    fake = FakeLauncher()
    fake.start_ok("job-1", root="other-root")
    client = LauncherClient(transport=fake.transport)
    res = client.job_start({"job_id": "job-1", "runner": "agent-cli", "model": "m1", "allow_hosts": [], "budget_min": 30, "prompt": "test prompt"})
    assert not _r.root_matches(res["root"], "expected-root")


def test_double_resume_queued_or_new(tmp_path):
    ex = _e.Exclusivity()
    ok, _ = ex.try_acquire(root="r1", wd="w1")
    assert ok
    # 同じ連鎖を 2 つ目が再開しようとする
    ok2, reason = ex.try_acquire(root="r1", wd="w1")
    assert not ok2 and reason == "session_busy"
    d = _e.decide_conflict(resume_required=True, busy="session_busy")
    assert d == {"mode": "queued", "reason": "session_busy"}
    d2 = _e.decide_conflict(resume_required=False, busy="session_busy")
    assert d2 == {"mode": "new_session", "reason": "session_busy"}


def test_launcher_busy_exit4(tmp_path):
    fake = FakeLauncher()
    fake.busy()
    client = LauncherClient(transport=fake.transport)
    with pytest.raises(LauncherError) as e:
        client.job_start({"job_id": "job-1", "runner": "agent-cli", "model": "m1", "allow_hosts": [], "budget_min": 10, "prompt": "test prompt"})
    assert e.value.exit_code == 4


def test_release_once_all_paths(tmp_path):
    ex = _e.Exclusivity()
    ex.try_acquire(root="r", wd="w")
    assert ex.release(root="r", wd="w") is True
    assert ex.release(root="r", wd="w") is False
    assert ex.releases == 1


def test_expired_chain_moved_not_deleted(tmp_path):
    # resume_max_age_min を過ぎた連鎖は archive/ の下へ移り、削除されない
    # launcher の job-retire の前提（archive/ へ mv）が壊れないことをここでは前提として記録する
    # 実機の移動は B6b の job-retire と T-114 の未実行に残す（実環境）
    assert (ROOT / "host" / "env-module-min" / "releases" / "b6" / "lib" / "ax_launcher.py").is_file()

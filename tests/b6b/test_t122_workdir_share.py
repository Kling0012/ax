"""T-122 作業の場所の共有と排他、続きからの再開（F-18.2、S-8.12）。launcher 側の部分。

worker-pool 側（同じ workdir_id の 2 つ目を queued(workdir_busy) で待たせる、jobs 表での (a)〜(d) の判定）は b6a の担当。
"""
import fcntl
import json
import os

from tests.b6b.helpers import flag_values

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "99999999-2222-3333-4444-555555555555"


def finish(sb, job_id, logs=""):
    sb.fd.finish(f"ax-job-{job_id}", logs=logs)
    code, out = sb.call("job-status", job_id)
    assert code == 0, out
    return out


def test_t122_same_workdir_id_shares_work_but_not_home(sb):
    assert sb.start("td1", model="m/a", workdir_id="item-1")[0] == 3  # 場所が無い明示は 3（git.worktree が先に作る）
    sb.work_dir("item-1").mkdir(parents=True)
    code, out = sb.start("td1", model="m/a", workdir_id="item-1")
    assert code == 0 and out == {"job_id": "td1", "root": "td1", "wd": "item-1"}
    finish(sb, "td1")
    code, out = sb.start("impl1", model="m/b", workdir_id="item-1")  # モデルが違ってもよい
    assert code == 0 and out["wd"] == "item-1" and out["root"] == "impl1"
    finish(sb, "impl1")
    code, out = sb.start("ver1", runner="verify-python", model=None, verify="true", workdir_id="item-1")
    assert code == 0 and out["wd"] == "item-1"
    for j in ("td1", "impl1", "ver1"):
        pass
    mounts = [flag_values(sb.run_calls()[i], "-v") for i in range(3)]
    assert all(m[0] == f"{sb.work_dir('item-1')}:/work:rw" for m in mounts)  # 同じ /work
    homes = {m[1] for m in mounts}
    assert len(homes) == 3  # 会話の記録（HOME）は共有されない
    labels = [flag_values(sb.run_calls()[i], "--label") for i in range(3)]
    assert all("ax.wd=item-1" in l for l in labels)


def test_t122_second_job_on_same_workdir_is_busy_exit_4(sb):
    sb.work_dir("item-1").mkdir(parents=True)
    assert sb.start("a1", workdir_id="item-1")[0] == 0
    code, out = sb.start("a2", model="m/other", workdir_id="item-1")  # launcher を直接呼んでも 4
    assert code == 4 and out["error"]["class"] == "busy" and out["error"]["reason"] == "workdir_busy"
    assert len(sb.run_calls()) == 1
    assert sb.fd.container("ax-job-a2") is None
    finish(sb, "a1")
    assert sb.start("a2", workdir_id="item-1")[0] == 0


def test_t122_flock_held_by_another_launcher_is_busy(sb):
    sb.work_dir("item-1").mkdir(parents=True)
    sb.state.joinpath("jobs").mkdir(exist_ok=True)
    fd = os.open(str(sb.state / "jobs" / ".wd-item-1.lock"), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out = sb.start("a1", workdir_id="item-1")
        assert code == 4
    finally:
        os.close(fd)
    assert sb.run_calls() == []
    assert sb.start("a1", workdir_id="item-1")[0] == 0  # 放した後は通る（排他は一度だけ解放される）


def test_t122_workdir_defaults(sb):
    code, out = sb.start("n1")
    assert code == 0 and out["wd"] == "n1" and sb.work_dir("n1").is_dir()  # 新しい場所は launcher が作る
    finish(sb, "n1")
    code, out = sb.start("n2", workdir_id="n1")
    assert code == 0 and out["wd"] == "n1" and out["root"] == "n2"
    assert (sb.state / "jobs" / "n2.wd").read_text().strip() == "n1"
    assert (sb.state / "jobs" / "n2.root").read_text().strip() == "n2"


def test_t122_container_log_is_per_job(sb):
    sb.work_dir("item-1").mkdir(parents=True)
    assert sb.start("a1", workdir_id="item-1")[0] == 0
    finish(sb, "a1", logs="log-a1\n")
    assert sb.start("a2", workdir_id="item-1")[0] == 0
    finish(sb, "a2", logs="log-a2\n")
    assert (sb.work_dir("item-1") / ".container-a1.log").read_text() == "log-a1\n"
    assert (sb.work_dir("item-1") / ".container-a2.log").read_text() == "log-a2\n"


def make_chain(sb, job_id="r1", sid=SID, wd=None):
    """連鎖の最初のジョブを動かして終わらせ、会話の記録を置く。"""
    kw = {"session_id": sid}
    if wd:
        sb.work_dir(wd).mkdir(parents=True, exist_ok=True)
        kw["workdir_id"] = wd
    assert sb.start(job_id, **kw)[0] == 0
    tr = sb.chain_dir(job_id) / "home" / ".claude" / "projects" / "-work"
    tr.mkdir(parents=True)
    (tr / f"{sid}.jsonl").write_text(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1}}}) + "\n")
    finish(sb, job_id)


def test_t122_resume_uses_original_chain_home_and_wd(sb):
    make_chain(sb, "r1", SID, wd="item-1")
    code, out = sb.start("r2", resume_from="r1", session_id=SID)
    assert code == 0, out
    assert out == {"job_id": "r2", "root": "r1", "wd": "item-1"}
    a = sb.run_args("r2")
    vs = flag_values(a, "-v")
    assert vs[0] == f"{sb.work_dir('item-1')}:/work:rw" and vs[1] == f"{sb.chain_dir('r1')}:/tmp-hdd:rw"
    assert "ax.chain=r1" in flag_values(a, "--label") and "ax.job=r2" in flag_values(a, "--label")
    assert flag_values(a, "--name") == ["ax-job-r2"]
    cmd = a[a.index("ax/agent-cli:test") + 1:][2]
    assert f"--resume {SID}" in cmd and "--session-id" not in cmd and "--fork-session" not in cmd
    assert (sb.state / "egress" / "job-r2.allow").is_file()
    assert (sb.state / "jobs" / "r2.root").read_text().strip() == "r1"
    # 連鎖の続き（r3 は r2 から。root は r1 のまま）
    finish(sb, "r2")
    code, out = sb.start("r3", resume_from="r2", session_id=SID)
    assert code == 0 and out["root"] == "r1" and out["wd"] == "item-1"
    # 元の許可先ファイルと別（ジョブごとに別の名前）
    assert (sb.state / "egress" / "job-r3.allow").is_file()


def test_t122_resume_with_matching_workdir_id_ok_and_mismatch_rejected(sb):
    make_chain(sb, "r1", SID, wd="item-1")
    sb.work_dir("item-2").mkdir()
    code, out = sb.start("r2", resume_from="r1", session_id=SID, workdir_id="item-2")
    assert code == 2 and out["error"]["reason"] == "workdir_mismatch"
    assert sb.fd.container("ax-job-r2") is None
    code, out = sb.start("r2", resume_from="r1", session_id=SID, workdir_id="item-1")
    assert code == 0 and out["wd"] == "item-1"


def test_t122_resume_without_record_or_transcript_is_exit_3(sb):
    code, out = sb.start("r2", resume_from="unknown", session_id=SID)
    assert code == 3 and out["error"]["reason"] == "session_not_found"
    make_chain(sb, "r1", SID)
    code, out = sb.start("r2", resume_from="r1", session_id=SID2)  # 別の session_id の記録は無い
    assert code == 3 and out["error"]["reason"] == "session_not_found"
    (sb.chain_dir("r1") / "home" / ".claude" / "projects" / "-work" / f"{SID}.jsonl").rename(sb.tmp / "moved.jsonl")
    code, out = sb.start("r2", resume_from="r1", session_id=SID)
    assert code == 3 and out["error"]["reason"] == "session_not_found"
    assert sb.fd.container("ax-job-r2") is None
    code, out = sb.start("r2", resume_from="r1")  # session_id が無い
    assert code == 2 and out["error"]["reason"] == "session_id_required"


def test_t122_resume_while_chain_running_is_busy(sb):
    make_chain(sb, "r1", SID)
    assert sb.start("r2", resume_from="r1", session_id=SID)[0] == 0
    code, out = sb.start("r3", resume_from="r1", session_id=SID)
    assert code == 4 and out["error"]["reason"] in ("session_busy", "workdir_busy")
    assert sb.fd.container("ax-job-r3") is None


def test_t122_new_conversation_uses_session_id_flag(sb):
    assert sb.start("n1", session_id=SID)[0] == 0
    cmd = sb.run_args("n1")[sb.run_args("n1").index("ax/agent-cli:test") + 1:][2]
    assert f"--session-id {SID}" in cmd and "--resume" not in cmd
    assert sb.start("n2")[0] == 0  # session_id なし（chat-json など）
    cmd2 = sb.run_args("n2")[sb.run_args("n2").index("ax/agent-cli:test") + 1:][2]
    assert "--session-id" not in cmd2 and "--resume" not in cmd2


def test_t122_job_retire_moves_chain_to_archive_not_delete(sb):
    make_chain(sb, "r1", SID)
    (sb.chain_dir("r1") / "marker.txt").write_text("keep")
    assert sb.start("r2", resume_from="r1", session_id=SID)[0] == 0
    code, out = sb.call("job-retire", "r2")
    assert code == 4 and out["error"]["reason"] == "chain_running"
    assert sb.chain_dir("r1").is_dir()
    finish(sb, "r2")
    code, out = sb.call("job-retire", "r2")
    assert code == 0 and out["root"] == "r1" and out["archived"]
    assert not sb.chain_dir("r1").exists()
    dest = sb.hddtmp / "archive"
    moved = list(dest.glob("job-r1-*"))
    assert len(moved) == 1 and (moved[0] / "marker.txt").read_text() == "keep"
    assert (moved[0] / "home" / ".claude" / "projects" / "-work" / f"{SID}.jsonl").is_file()
    code, out = sb.call("job-retire", "r2")  # 冪等
    assert code == 0 and out["archived"] is None
    assert sb.call("job-retire", "nosuch")[0] == 3
    assert sb.work_dir("r1").is_dir()  # /work は動かさない


def test_t122_resume_after_retire_is_session_not_found(sb):
    make_chain(sb, "r1", SID)
    assert sb.call("job-retire", "r1")[0] == 0
    code, out = sb.start("r2", resume_from="r1", session_id=SID)
    assert code == 3 and out["error"]["reason"] == "session_not_found"


def test_t122_start_failure_releases_locks(sb):
    sb.work_dir("item-1").mkdir(parents=True)
    req = {"job_id": "a1", "runner": "agent-cli", "model": "m", "allow_hosts": [], "budget_min": 5, "workdir_id": "item-1"}
    code, out = sb.call("job-start", "a1", stdin=req, extra_env={"FAKE_DOCKER_FAIL_RUN": "1"})
    assert code == 70
    assert sb.start("a1b", workdir_id="item-1")[0] == 0  # 失敗した起動が排他を握ったままにならない

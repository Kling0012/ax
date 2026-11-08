"""T-11 TDD（S-7.6、D-6.6）。直接出す役割がリポジトリを書き換えようとすると拒否される（書き換えは work-tdd だけ）。test_design（Flash 系）、impl（GLM）、
review と verify.cmd が、モデルが違っても git.worktree の同じ workdir_id で 1 つの worktree を見て、gate.red と verify.cmd の対象に届く。
最初から通るテストが差し戻される。実装がテストを変えると gate.tests_unchanged で bug になる。"""
from pathlib import Path

from runtime.subloop.inbox import accept_item
from tests.b7.exhelp import graph, make_executor, manifest, node
from tests.b7.helpers import run

TESTS = "tests/ax_generated/todo-1"
RED_FAIL = {"state": "failed", "verify": {"rc": 1, "log_tail": "FAILED tests/x.py::test_a\n=== 1 failed in 0.10s ==="}}
RED_PASS = {"state": "ok", "verify": {"rc": 0, "log_tail": "=== 1 passed in 0.10s ==="}}


def item(**over):
    d = {"item_key": "todo-1", "todo_id": "todo-1", "kind": "work", "project": "demo", "role": "impl", "title": "足し算を追加する", "body": "add(a, b) を作る",
         "acceptance": {"examples": ["add(1, 2) は 3"], "boundaries": ["既存の関数名を変えない"]}, "base_ref": "main", "branch": "ai/todo-1", "verify": "pytest -q"}
    d.update(over)
    return d


def write_test(body, wd):
    d = Path(wd) / "tests" / "ax_generated" / body["item_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "test_a.py").write_text("def test_a():\n    assert 1 + 1 == 2\n", encoding="utf-8")


def setup(env, it=None, models=True):
    ex = env.work_tdd()
    code, body = accept_item(ex, it or item())
    assert code == 200, body
    env.pool.effects["test_design"] = write_test
    if models:
        env.pool.script("test_design", {"model": "flash-model"})
        env.pool.script("impl", {"model": "glm-model"})
        env.pool.script("review", {"model": "other-model"})
    env.pool.script("verify", RED_FAIL)      # 最初の verify のジョブ = gate.red（赤になる）
    return ex


def go(ex):
    async def _go():
        await ex.run_once()
        await ex.drain()

    run(_go())


def test_all_jobs_share_one_worktree_across_models_and_the_item_completes(env):
    ex = setup(env)
    go(ex)
    it = ex.store.get_item("todo-1")
    assert it["state"] == "completed", it
    wd = ex.store.node_outputs("todo-1")["tdd-change.worktree"]["data"]["workdir_id"]
    assert wd.startswith("wt-")
    roles = [b["role"] for b in env.pool.submitted]
    assert roles == ["test_design", "verify", "impl", "verify", "review"]            # gate.red と verify.cmd は verify のジョブ。skip の関門は出さない
    assert {b["workdir_id"] for b in env.pool.submitted} == {wd}                       # 同じ workdir_id（モデルが違っても）
    assert [j["model"] for j in env.pool.jobs.values() if j["role"] != "verify"] == ["flash-model", "glm-model", "other-model"]
    assert all("resume_from" not in b for b in env.pool.submitted)                      # 会話は共有しない（入り直しの無い初回に resume_from は付かない）
    assert (env.work_root / f"job-{wd}" / ".git").exists()                              # git.worktree が作った場所
    # gate.red の verify のジョブは、実装の前（impl より前）
    assert env.pool.submitted[1]["verify"] == "pytest -q" and env.pool.submitted[3]["verify"] == "pytest -q"


def test_prompts_carry_the_procedure_acceptance_and_standing_rules(env):
    ex = setup(env, item(answer="A 案でよい"))
    go(ex)
    td, im = env.pool.by_role("test_design")[0], env.pool.by_role("impl")[0]
    assert td["attachments"] == ["docs/test-procedure.md"]
    for must in ("add(1, 2) は 3", "既存の関数名を変えない", "テストの件数の上限", "12", f"{TESTS}/", "A 案でよい"):
        assert must in td["prompt"], must
    assert "テストを書かない・変えない" in im["prompt"]
    assert td["item_id"] == "todo-1" and td["todo_id"] == "todo-1" and td["subloop"] == "work-tdd" and td["sensitivity"] == "private"
    assert "attachments" not in im


def test_a_test_that_passes_from_the_start_is_sent_back_to_test_design(env):
    ex = setup(env)
    env.pool.scripts["verify"] = [RED_PASS, RED_FAIL]       # 1 回目の gate.red: 最初から通る → bug → test_design へ。2 回目は赤
    go(ex)
    assert ex.store.get_item("todo-1")["state"] == "completed"
    assert [b["role"] for b in env.pool.submitted][:5] == ["test_design", "verify", "test_design", "verify", "impl"]
    td2 = env.pool.by_role("test_design")[1]
    assert td2["resume_from"] == "job-1"                    # 同じノードに入り直すとき、前の試行のジョブの ID（S-8.12 の 8）
    red_runs = ex.store.node_runs(node_id="tdd-change.red")
    assert [r["status"] for r in red_runs] == ["bug", "ok"] and red_runs[0]["out"]["error"]["code"] in ("red_all_pass", "red_already_passing")


def test_red_for_a_wrong_reason_is_also_rejected(env):
    ex = setup(env)
    env.pool.scripts["verify"] = [{"state": "failed", "verify": {"rc": 2, "log_tail": "ImportError: cannot import name add\n=== 1 error in 0.1s ==="}}, RED_FAIL]
    go(ex)
    red_runs = ex.store.node_runs(node_id="tdd-change.red")
    assert red_runs[0]["out"]["error"]["code"] == "red_invalid_reason" and red_runs[1]["status"] == "ok"


def test_implementation_that_changes_the_tests_is_a_bug_and_goes_back_to_impl(env):
    ex = setup(env)
    calls = {"n": 0}

    def impl_effect(body, wd):
        calls["n"] += 1
        if calls["n"] == 1:
            (Path(wd) / TESTS / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")     # テストを書き換える
        else:
            write_test(body, wd)                                                                                 # 差し戻された実装は元のテストに戻す

    env.pool.effects["impl"] = impl_effect
    env.pool.script("impl", {"model": "glm-model"})
    go(ex)
    tu = ex.store.node_runs(node_id="tdd-change.tests_unchanged")
    assert tu[0]["status"] == "bug" and tu[0]["out"]["error"]["code"] == "tests_changed" and tu[0]["out"]["data"]["changed"]
    assert tu[1]["status"] == "ok"
    imp = env.pool.by_role("impl")
    assert len(imp) == 2 and imp[1]["resume_from"] == "job-3"       # impl への差し戻しは、前の impl のジョブから続ける
    assert ex.store.get_item("todo-1")["state"] == "completed"


def test_deleted_or_added_tests_count_as_changes(env):
    ex = setup(env)

    def impl_effect(body, wd):
        (Path(wd) / TESTS / "test_new.py").write_text("x = 1\n", encoding="utf-8")

    env.pool.effects["impl"] = impl_effect
    env.pool.scripts["impl"] = [{}, {}]
    env.pool.effects["impl"] = lambda body, wd: ((Path(wd) / TESTS / "test_new.py").write_text("x", encoding="utf-8") if len(env.pool.by_role("impl")) == 1 else (Path(wd) / TESTS / "test_new.py").unlink())
    go(ex)
    tu = ex.store.node_runs(node_id="tdd-change.tests_unchanged")
    assert tu[0]["out"]["data"]["added"] == [f"{TESTS}/test_new.py"] and tu[-1]["status"] == "ok"


def test_verify_failure_goes_back_to_impl_until_max_loops_then_ejects(env):
    ex = setup(env)
    env.pool.scripts["verify"] = [RED_FAIL] + [{"state": "failed", "verify": {"rc": 1, "log_tail": "=== 1 failed in 0.1s ==="}}] * 10
    go(ex)
    it = ex.store.get_item("todo-1")
    assert it["state"] == "ejected" and it["reason"].startswith("max_loops")
    assert len(env.pool.by_role("impl")) == 4                       # 最初の 1 回 + 差し戻し 3 回（tdd_impl_loops の既定 3）
    ev = env.sink.of("item.state", state="ejected")
    assert ev[0]["data"]["todo_id"] == "todo-1" and not env.ledger.created and not env.ledger.transitions    # todo_id のある項目は ToDo を作らず、遷移も呼ばない


def test_assist_cannot_write_the_repository(env):
    g = graph([node("a", "worker.run", {"role": "assist", "prompt": "調べる", "workdir_id": "wt-abc"}, on_fail="eject")], [])
    man = manifest(roles=["assist"])

    async def _go():
        ex = make_executor(env, g, man)
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(_go())
    it = ex.store.items()[0]
    assert it["state"] == "ejected" and it["reason"].endswith("role_cannot_write") and not env.pool.submitted        # worker-pool に出す前に拒否する


def test_roles_that_write_need_a_workdir(env):
    g = graph([node("a", "worker.run", {"role": "impl", "prompt": "x"}, on_fail="eject")], [])

    async def _go():
        ex = make_executor(env, g, manifest())
        await ex.run_once()
        await ex.drain()
        return ex

    ex = run(_go())
    assert ex.store.items()[0]["reason"].endswith("workdir_required")


def test_blocked_job_ejects_with_resume_ref_and_waiting_reason_for_an_item_with_a_todo(env):
    ex = setup(env)
    env.pool.scripts["impl"] = [{"model": "glm-model", "state": "failed", "report": {"status": "blocked", "human_action": "A 案と B 案のどちらにしますか", "waiting_reason": "preference"},
                                 "verify": {"rc": None}, "ended_at": "2026-11-05T12:30:00.000Z"}]
    go(ex)
    it = ex.store.get_item("todo-1")
    assert it["state"] == "ejected" and it["reason"] == "blocked"
    ev = env.sink.of("item.state", state="ejected")[-1]["data"]
    assert ev["todo_id"] == "todo-1"
    rr = ev["artifacts"]["resume_ref"]
    assert rr["job_id"] == "job-3" and rr["model"] == "glm-model" and rr["workdir_id"].startswith("wt-") and rr["ended_at"] == "2026-11-05T12:30:00.000Z"
    assert ev["artifacts"]["waiting_reason"] == "preference" and ev["artifacts"]["human_action"].startswith("A 案")
    assert not env.ledger.created and not env.ledger.transitions            # 台帳が item.state を読んで元の ToDo を遷移させる。サブループは呼べない


def test_blocked_job_of_an_item_without_todo_creates_waiting_user_or_parked(env):
    # todo_id の無い項目（巡回で見つけた物）は ToDo を作る。理由が waiting_reason に当たれば waiting_user、当たらなければ parked
    for wr, state in (("preference", "waiting_user"), (None, "parked")):
        env.ledger.created.clear()
        env.ledger.todos.clear()
        g = graph([node("a", "worker.run", {"role": "review", "prompt": "x", "workdir_id": "wt-a"}, on_fail="eject")], [])
        env.pool.scripts["review"] = [{"state": "ok", "report": {"status": "blocked", "human_action": "どうしますか", "waiting_reason": wr}}]
        man = manifest(roles=["review"])

        async def _go():
            ex = make_executor(env, g, man)
            await ex.run_once()
            await ex.drain()
            return ex

        run(_go())
        t = env.ledger.created[0]
        assert t["state"] == state and t["resume_ref"]["job_id"].startswith("job-")
        if state == "waiting_user":
            assert t["card"]["waiting_reason"] == "preference" and t["card"]["question"] == "どうしますか"
        else:
            assert t["reason"] == "blocked" and t["check_at"]


def test_resume_after_the_users_answer_starts_from_the_blocked_job_and_reuses_the_worktree(env):
    rr = {"job_id": "job-77", "workdir_id": "wt-keep", "model": "glm-model", "ended_at": "2026-11-05T12:30:00.000Z"}
    (env.work_root / "job-wt-keep" / ".git").parent.mkdir(parents=True)
    (env.work_root / "job-wt-keep" / ".git").write_text("gitdir: x")
    ex = setup(env, item(answer="B 案にする", resume_ref=rr))
    go(ex)
    outs = ex.store.node_outputs("todo-1")
    assert outs["tdd-change.worktree"]["data"]["workdir_id"] == "wt-keep" and outs["tdd-change.worktree"]["data"]["reused"] is True
    first = env.pool.submitted[0]
    assert first["workdir_id"] == "wt-keep" and first["resume_from"] == "job-77" and first["resume_required"] is True and "B 案にする" in first["prompt"]
    assert ex.store.get_item("todo-1")["state"] == "completed"


def test_item_without_verify_still_goes_through_with_skipped_command_gates(env):
    ex = setup(env, item(verify=""))
    env.pool.scripts["verify"] = []
    go(ex)
    assert ex.store.get_item("todo-1")["state"] == "completed"
    assert [b["role"] for b in env.pool.submitted] == ["test_design", "impl", "review"]
    assert ex.store.node_runs(node_id="tdd-change.red")[0]["status"] == "skip"
    assert ex.store.node_runs(node_id="tdd-change.verify")[0]["status"] == "skip"


def _with_gate_cmds(env, **cmds):
    """work-tdd のグラフの tdd-change に、関門のコマンドを渡す（項目の形 work-item は固定なので、グラフの with で渡す）。"""
    from runtime.subloop.graph import load_yaml

    d = env.root / "plugins" / "subloop" / "work-tdd"
    doc = load_yaml(d / "graph.yaml")
    inc = next(n for n in doc["nodes"] if n.get("include"))
    inc["with"].update(cmds)
    man = load_yaml(d / "manifest.yaml")
    env.svc.plugin_dir = d
    ex = env.executor(doc, man)
    assert accept_item(ex, item())[0] == 200
    env.pool.effects["test_design"] = write_test
    return ex


def test_optional_gates_run_when_the_graph_supplies_commands(env):
    ex = _with_gate_cmds(env, coverage_cmd="cov", lint_cmd="lint", types_cmd="mypy", mutation_cmd="mut")
    env.pool.scripts["verify"] = [RED_FAIL, {}, {"verify": {"rc": 0, "log_tail": "changed lines: 91%"}}, {}, {}, {}]
    go(ex)
    assert ex.store.get_item("todo-1")["state"] == "completed"
    assert [b["verify"] for b in env.pool.by_role("verify")] == ["pytest -q", "pytest -q", "cov", "lint", "mypy", "mut"]
    cov = ex.store.node_runs(node_id="tdd-change.coverage")[0]["out"]["data"]
    assert cov["coverage"] == 0.91 and cov["threshold"] == 0.8


def test_low_coverage_goes_back_to_impl(env):
    ex = _with_gate_cmds(env, coverage_cmd="cov")
    env.pool.scripts["verify"] = [RED_FAIL, {}, {"verify": {"rc": 0, "log_tail": "changed lines: 40%"}}, {}, {"verify": {"rc": 0, "log_tail": "changed lines: 95%"}}]
    go(ex)
    cov = ex.store.node_runs(node_id="tdd-change.coverage")
    assert cov[0]["status"] == "bug" and cov[0]["out"]["error"]["code"] == "coverage_low" and cov[1]["status"] == "ok"
    assert len(env.pool.by_role("impl")) == 2 and ex.store.get_item("todo-1")["state"] == "completed"


def test_job_rejection_and_pool_down_are_system_errors_not_bugs(env):
    ex = setup(env)
    env.pool.reject_next = [(429, "rate_limited")]
    go(ex)
    runs = ex.store.node_runs(node_id="tdd-change.test_design")
    assert runs[0]["status"] == "error" and runs[0]["out"]["error"]["class"] == "system.expected"       # 再試行の対象
    assert runs[-1]["status"] == "ok"

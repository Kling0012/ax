"""部品庫（plugins/nodes/<名前空間>/<型>.py）の型ごとの試験（S-7.4、D-6.3）。偽の部品だけを使う。"""
import json

import pytest

from axcommon.client import ApiError, Unavailable
from runtime.subloop import nodelib, template
from runtime.subloop.nodes import declared_node_dirs, load_registry, subloop_registry
from tests.b7.exhelp import ROOT, graph, make_executor, manifest, node
from tests.b7.helpers import run
from tests.fakes.b7.parts import CmdResult

EXPECTED_TYPES = """gh.list_issues gh.list_prs gh.comment gh.pr_open gh.merge gh.label git.worktree inbox.take git.commit git.push shell.run ci.trigger ci.collect
worker.run env.run env.cancel decide.llm verify.cmd gate.tests_unchanged gate.red gate.coverage_changed gate.lint gate.types gate.mutation gate.secret_scan
ledger.add ledger.wait notify wait.event eigen.outbox_check eigen.preflight eigen.record x.posts""".split()


def one(env, typ, with_, *, item=None, man=None, post=None):
    """型 1 つだけのグラフを単一項目で走らせ、(項目, ノードの実行記録) を返す。"""
    g = graph([node("n", typ, with_, on_fail="eject")], [])
    ex = make_executor(env, g, man or manifest(roles=["review", "assist", "screen", "impl"]))
    if post:
        post(ex)

    async def go():
        rid = await ex.run_once()
        await ex.drain()
        return rid

    rid = run(go())
    it = ex.store.items(run_id=rid)[0]
    return it, ex.store.node_runs(item_id=it["item_id"], node_id="n"), ex


def out(env, typ, with_, **kw):
    it, nrs, ex = one(env, typ, with_, **kw)
    return nrs[-1]["status"], nrs[-1]["out"], it


# ---- 部品庫 ----
def test_every_type_in_s74_is_loaded_with_a_valid_schema_and_meta():
    import jsonschema

    reg = load_registry(ROOT / "plugins" / "nodes")
    assert reg.failed == {}
    assert sorted(reg.names()) == sorted(EXPECTED_TYPES)
    meta_schema = json.loads((ROOT / "schemas" / "ext" / "b7" / "node-type-meta.schema.json").read_text(encoding="utf-8"))
    for name, t in reg.types.items():
        jsonschema.Draft202012Validator.check_schema(t.schema)
        jsonschema.validate(t.meta, meta_schema)
        assert t.schema.get("type") == "object" and callable(t.run), name
        if t.meta["launching"]:
            assert t.reconcile is not None, name
    # 外へ影響する型は outputs を宣言する。公開する型は secret_scan を要る
    assert {n for n, t in reg.types.items() if t.meta["output"]} == {"gh.comment", "gh.pr_open", "gh.merge", "gh.label", "git.push", "ci.trigger"}
    assert {n for n, t in reg.types.items() if t.meta["publishes"]} == {"gh.comment", "gh.pr_open"}
    assert {n for n, t in reg.types.items() if t.meta["llm"]} == {"worker.run", "decide.llm"}      # LLM を使う型は worker.run と decide.llm だけ


def test_a_broken_type_only_disables_itself(tmp_path):
    d = tmp_path / "nodes" / "x"
    d.mkdir(parents=True)
    (d / "bad.py").write_text("raise RuntimeError('x')\n", encoding="utf-8")
    (d / "norun.py").write_text("schema = {}\n", encoding="utf-8")
    (d / "launch.py").write_text("schema = {}\nasync def run(c, i): pass\nMETA = {'launching': True}\n", encoding="utf-8")
    (d / "good.py").write_text("schema = {'type': 'object'}\nasync def run(c, i): return {'status': 'ok'}\n", encoding="utf-8")
    reg = load_registry(ROOT / "plugins" / "nodes", tmp_path / "nodes")
    assert "x.good" in reg and set(reg.failed) == {"x.bad", "x.norun", "x.launch"} and "gh.comment" in reg


def test_duplicate_type_name_in_a_later_directory_is_rejected(tmp_path):
    d = tmp_path / "nodes" / "gh"
    d.mkdir(parents=True)
    (d / "comment.py").write_text("schema = {'type': 'object'}\nasync def run(c, i): return {'status': 'ok'}\n", encoding="utf-8")
    reg = load_registry(ROOT / "plugins" / "nodes", tmp_path / "nodes")
    assert reg.failed == {"gh.comment": "duplicate"}
    assert reg.get("gh.comment").meta["output"] == "github.comment"       # 先に読んだ部品庫の物が残る


# ---- 宣言的な node_dirs（D-6.3 の差し込み口。nodes/ を置けないサブループが自分の担当パスの中で宣言する） ----
def _type_file(d, name="extra"):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.py").write_text(
        "schema = {'type': 'object'}\n"
        "async def run(c, i):\n    from runtime.subloop.nodeapi import ok\n    return ok()\n",
        encoding="utf-8")


def test_declared_node_dirs_are_read(tmp_path):
    sub = tmp_path / "sub"
    (sub / "integration" / "myns").mkdir(parents=True)
    _type_file(sub / "integration" / "myns")
    (sub / "nodes.yaml").write_text("node_dirs: [integration]\n", encoding="utf-8")
    reg = subloop_registry(ROOT, sub)
    assert "myns.extra" in reg and reg.failed == {} and "gh.comment" in reg


def test_node_dirs_declaration_is_relative_to_its_own_file(tmp_path):
    sub = tmp_path / "sub"
    (sub / "integration").mkdir(parents=True)
    (sub / "integration" / "nodes" / "ns2").mkdir(parents=True)
    _type_file(sub / "integration" / "nodes" / "ns2", "two")
    (sub / "integration" / "nodes.yaml").write_text("node_dirs: [nodes]\n", encoding="utf-8")
    reg = subloop_registry(ROOT, sub)
    assert "ns2.two" in reg and reg.failed == {}


def test_subloop_own_nodes_come_before_the_declared_dirs(tmp_path):
    sub = tmp_path / "sub"
    (sub / "nodes" / "own").mkdir(parents=True)
    _type_file(sub / "nodes" / "own", "first")
    (sub / "integration" / "own").mkdir(parents=True)
    _type_file(sub / "integration" / "own", "first")                     # 同じ型名
    (sub / "nodes.yaml").write_text("node_dirs: [integration]\n", encoding="utf-8")
    reg = subloop_registry(ROOT, sub)
    assert "own.first" in reg and reg.failed == {"own.first": "duplicate"}   # 先に読んだ nodes/ が残る


def test_missing_or_invalid_node_dirs_are_skipped_and_paths_stay_relative(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nodes.yaml").write_text("node_dirs: [/abs/path, ../outside, 'a\\\\b', 3]\n", encoding="utf-8")
    (sub / "broken.yaml").write_text("::: not yaml [\n", encoding="utf-8")
    (sub / "novalue.yaml").write_text("other: 1\n", encoding="utf-8")
    assert declared_node_dirs(sub) == []                                 # どの宣言も使えない → 空（fail-soft）
    reg = subloop_registry(ROOT, sub)
    assert reg.failed == {} and "gh.comment" in reg                      # 共通の部品庫は普通に読める


def test_declared_dirs_are_deduplicated_and_missing_dirs_are_fine(tmp_path):
    sub = tmp_path / "sub"
    (sub / "integration").mkdir(parents=True)
    (sub / "nodes.yaml").write_text("node_dirs: [integration, integration, gone]\n", encoding="utf-8")
    dirs = declared_node_dirs(sub)
    assert len(dirs) == 2 and (sub / "integration").resolve() in dirs    # 無い置き場（gone）も素通りで返す
    reg = subloop_registry(ROOT, sub)
    assert "gh.comment" in reg and reg.failed == {}


# ---- テンプレート ----
def test_template_rendering_rules():
    sc = {"knobs": {"n": 3, "s": "x"}, "item": {"a": {"b": "deep"}, "none": None}, "nodes": {"p.q": {"data": {"v": 7}}, "p": {"data": {"v": 1}}}}
    assert template.render("{{knobs.n}}", sc) == 3                           # 全文が参照なら型を保つ
    assert template.render("n={{knobs.n}}/{{knobs.s}}", sc) == "n=3/x"
    assert template.render("{{item.a.b}}", sc) == "deep" and template.render("[{{item.none}}]", sc) == "[]"
    assert template.render("{{item.missing}}", sc) is None                   # item の欠けは None（任意の項目）
    assert template.render("{{nodes.p.q.data.v}}", sc) == 7                  # 最長の ID を選ぶ
    assert template.render("{{nodes.p.data.v}}", sc) == 1
    with pytest.raises(template.TemplateError):
        template.render("{{knobs.zzz}}", sc)
    with pytest.raises(template.TemplateError):
        template.render("{{other.x}}", sc)
    assert template.render({"a": ["{{knobs.n}}", 1]}, sc) == {"a": [3, 1]}
    assert template.render("{{params.x}}-{{knobs.n}}", sc, partial=True) == "{{params.x}}-3"
    assert template.references({"a": ["{{knobs.n}}", "x {{item.y}}"]}) == ["knobs.n", "item.y"]


def test_parse_test_counts_and_last_json():
    assert nodelib.parse_test_counts("=== 2 failed, 1 passed in 0.5s ===") == {"passed": 1, "failed": 2, "errors": 0}
    assert nodelib.parse_test_counts("=== 3 passed in 0.5s ===")["failed"] == 0
    assert nodelib.parse_test_counts("Tests:       1 failed, 4 passed, 5 total")["passed"] == 4
    assert nodelib.parse_test_counts("Failed!  - Failed:     2, Passed:     5, Skipped:     0")["failed"] == 2
    assert nodelib.parse_test_counts("???") == {"passed": None, "failed": None, "errors": None}
    from importlib import util

    spec = util.spec_from_file_location("dl", ROOT / "plugins/nodes/decide/llm.py")
    m = util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.last_json('前置き {"a": 1} 途中 {"b": {"c": "}"}} 末尾') == {"b": {"c": "}"}}
    assert m.last_json("無し") is None


# ---- gh ----
def test_gh_list_issues_and_prs(env):
    env.github.issues["o/r"] = [{"number": 5, "title": "T", "body": None, "html_url": "u", "labels": [{"name": "ai-ready"}]}]
    env.github.prs["o/r"] = {9: {"number": 9, "title": "P", "head": {"ref": "h", "sha": "s"}, "base": {"ref": "main"}, "html_url": "pu"}}
    st, o, it = out(env, "gh.list_issues", {"repo": "o/r", "label": "ai-ready"})
    assert st == "ok" and o["data"]["items"][0]["item_id"] == "o/r#5" and o["data"]["items"][0]["labels"] == ["ai-ready"]
    st, o, _ = out(env, "gh.list_prs", {"repo": "o/r"})
    assert o["data"]["items"][0] == {"item_id": "o/r!9", "repo": "o/r", "number": 9, "title": "P", "head": "h", "head_sha": "s", "base": "main", "url": "pu"}


def test_github_errors_are_classified(env):
    env.github.fail = [ApiError(429, "rate")]
    st, o, _ = out(env, "gh.list_issues", {"repo": "o/r"})
    assert st == "ok"
    nrs = one(env, "gh.list_issues", {"repo": "o/r"}, post=lambda ex: setattr(env.github, "fail", [ApiError(429, "rate")] * 4))[1]
    assert nrs[0]["out"]["error"] == {"class": "system.expected", "code": "http_429", "message": "rate"}      # 再試行の対象
    assert len(nrs) == 4                                                                                          # 既定の system_retries(3) + 最初の 1 回
    env.github.fail = [ApiError(404, "nf")]
    st, o, _ = out(env, "gh.list_issues", {"repo": "o/r"})
    assert st == "error" and o["error"]["class"] == "unknown" and o["error"]["code"] == "http_404"
    env.svc.github = None
    st, o, _ = out(env, "gh.list_issues", {"repo": "o/r"})
    assert o["error"]["code"] == "github_unavailable"


def test_gh_comment_writes_a_marker_and_launching_before_the_post(env):
    st, o, it = out(env, "gh.comment", {"repo": "o/r", "number": 3, "body": "本文"})
    assert st == "ok"
    c = env.github.comments[("o/r", 3)]
    assert len(c) == 1 and c[0]["body"].startswith("本文\n\n<!-- ax:gh-comment-") and env.github.posted == [("comment", "o/r", 3)]
    nr = [e for e in env.sink.of("node.state") if e["data"]["state"] == "launching"]
    assert len(nr) == 1 and nr[0]["data"]["key"].startswith("gh-comment-")


def test_gh_comment_does_not_repost_when_the_marker_exists(env):
    it, nrs, ex = one(env, "gh.comment", {"repo": "o/r", "number": 3, "body": "本文"})
    key = nrs[0]["launching_key"]
    n_before = len(env.github.posted)

    async def again():
        # 同じ項目・同じノードの 2 度目の実行（再試行など）。印があれば書かない
        from runtime.subloop.executor import Result  # noqa: F401

        ex.store.set_item_state(it["item_id"], "planned")
        ex.store.update_item(it["item_id"], started=False)
        await ex._pump()
        await ex.drain()

    run(again())
    assert len(env.github.posted) == n_before and key and ex.store.node_runs(node_id="n")[-1]["out"]["data"]["reused"] is True


def test_gh_pr_open_reuses_existing_pr_and_merge_checks_head(env):
    st, o, _ = out(env, "gh.pr_open", {"repo": "o/r", "head": "ai/x", "base": "main", "title": "T"})
    assert st == "ok" and o["data"]["number"] == 1 and env.github.posted == [("pr_open", "o/r", "ai/x")]
    st, o, _ = out(env, "gh.pr_open", {"repo": "o/r", "head": "ai/x", "base": "main", "title": "T"})
    assert o["data"]["reused"] is True and len(env.github.posted) == 1
    sha = "a" * 40
    st, o, _ = out(env, "gh.merge", {"repo": "o/r", "number": 1, "sha": "b" * 40})
    assert st == "bug" and o["error"]["code"] == "head_mismatch" and ("merge", "o/r", 1) not in env.github.posted      # head が違えばマージしない
    st, o, _ = out(env, "gh.merge", {"repo": "o/r", "number": 1, "sha": sha})
    assert st == "ok" and ("merge", "o/r", 1) in env.github.posted
    n = len(env.github.posted)
    st, o, _ = out(env, "gh.merge", {"repo": "o/r", "number": 1, "sha": sha})
    assert o["data"]["reused"] is True and len(env.github.posted) == n
    env.github.prs["o/r"][1]["merged"] = False
    env.github.fail = [ApiError(405, "not mergeable")]
    orig = env.github.get_pr
    st, o, _ = out(env, "gh.merge", {"repo": "o/r", "number": 1, "sha": sha}, post=lambda ex: None)
    assert st in ("bug", "error")


def test_gh_label_adds_only_missing_labels(env):
    env.github.labels_of[("o/r", 4)] = {"a"}
    st, o, _ = out(env, "gh.label", {"repo": "o/r", "number": 4, "add": ["a", "b"]})
    assert o["data"]["added"] == ["b"] and env.github.labels_of[("o/r", 4)] == {"a", "b"}
    st, o, _ = out(env, "gh.label", {"repo": "o/r", "number": 4, "add": ["a", "b"]})
    assert o["data"]["added"] == [] and len(env.github.posted) == 1


# ---- git ----
def test_git_worktree_creates_and_reuses(env):
    st, o, it = out(env, "git.worktree", {"repo": "demo", "base_ref": "main", "branch": "ai/x"})
    d = o["data"]
    assert st == "ok" and d["workdir_id"].startswith("wt-") and d["branch"] == "ai/x" and d["base_sha"] == env.git.head_sha
    assert (env.work_root / f"job-{d['workdir_id']}" / ".git").exists() and (env.work_root / "repos" / "demo.git" / "HEAD").exists()
    cmds = [c[0][0] for c in env.git.calls]
    assert cmds[:3] == ["clone", "worktree", "rev-parse"]
    st, o2, _ = out(env, "git.worktree", {"repo": "demo", "base_ref": "main", "branch": "ai/y"})
    assert "fetch" in [c[0][0] for c in env.git.calls]                      # 2 回目からは clone でなく fetch
    assert o2["data"]["workdir_id"] != d["workdir_id"]                      # 項目ごとに別の worktree（S-7.5）


def test_git_worktree_errors(env):
    st, o, _ = out(env, "git.worktree", {"repo": "unknown-project", "base_ref": "main", "branch": "ai/x"})
    assert st == "error" and o["error"]["code"] == "repo_unregistered"
    st, o, _ = out(env, "git.worktree", {"repo": "demo", "base_ref": "main", "branch": "ai/../x"})
    assert st == "bug" and o["error"]["code"] == "branch_invalid"
    env.git.fail_on["clone"] = CmdResult(128, "", "fatal")
    st, o, _ = out(env, "git.worktree", {"repo": "demo", "base_ref": "main", "branch": "ai/x"})
    assert o["error"]["code"] == "clone_failed" and o["error"]["class"] == "system.expected"


def test_git_commit_and_push_rules(env):
    wd = env.work_root / "job-wt-a"
    wd.mkdir()
    env.git.dirty[str(wd)] = True
    st, o, _ = out(env, "git.commit", {"workdir_id": "wt-a", "message": "m"})
    assert st == "ok" and o["data"]["committed"] is True
    st, o, _ = out(env, "git.commit", {"workdir_id": "wt-a", "message": "m"})
    assert st == "skip"                                                     # 変更が無ければ skip
    for remote, branch, code in (("ax.git", "ai/x", "push_forbidden"), ("work.git", "main", "push_forbidden"), ("work.git", "master", "push_forbidden"),
                                 ("origin", "ai/x", "push_forbidden"), ("nowhere", "ai/x", "remote_unregistered")):
        st, o, _ = out(env, "git.push", {"remote": remote, "branch": branch, "workdir_id": "wt-a"})
        assert st == "error" and o["error"]["code"] == code, (remote, branch)
    assert not env.git.pushes()
    env.git.dirty[str(wd)] = True
    st, o, it = out(env, "git.push", {"remote": "work.git", "branch": "ai/x", "workdir_id": "wt-a", "commit_message": "ax: t"})
    assert st == "ok" and len(env.git.pushes()) == 1 and env.git.pushes()[0][0][1:] == ["/git/work.git", "+HEAD:refs/heads/ai/x"]
    assert env.git.remote_heads["/git/work.git"]["ai/x"] == o["data"]["sha"] and it["artifacts"]["branch"] == "ai/x"
    env.git.fail_on["push"] = CmdResult(1, "", "rejected")
    st, o, _ = out(env, "git.push", {"remote": "work.git", "branch": "ai/x", "workdir_id": "wt-a"})
    assert o["error"]["code"] == "push_failed" and o["error"]["class"] == "system.expected"


def test_shell_run(env):
    st, o, _ = out(env, "shell.run", {"cmd": "make ci", "workdir_id": "wt-a"})
    assert st == "ok" and env.shell.calls == [("make ci", str(env.work_root / "job-wt-a"))]
    env.shell.results = [CmdResult(2, "", "bad")]
    st, o, _ = out(env, "shell.run", {"cmd": "make ci"})
    assert st == "bug" and o["data"]["rc"] == 2 and o["error"]["code"] == "nonzero_exit"


def test_ci_trigger_and_collect(env):
    st, o, _ = out(env, "ci.trigger", {"repo": "o/r", "workflow": "ci.yml", "ref": "main"})
    marker = o["data"]["marker"]
    assert st == "ok" and env.github.dispatched[0][3]["ax_marker"] == marker
    st, o, _ = out(env, "ci.collect", {"repo": "o/r", "marker": marker})
    assert st == "ok" and o["data"]["conclusion"] == "success"
    env.github.runs[0]["conclusion"] = "failure"
    st, o, _ = out(env, "ci.collect", {"repo": "o/r", "marker": marker})
    assert st == "bug" and o["error"]["code"] == "ci_failed"
    env.github.runs[0]["status"] = "in_progress"
    nrs = one(env, "ci.collect", {"repo": "o/r", "marker": marker})[1]
    assert nrs[0]["out"]["error"]["code"] == "ci_running" and nrs[0]["out"]["error"]["class"] == "system.expected"


# ---- worker / decide / verify ----
def test_worker_run_result_mapping(env):
    man = manifest(roles=["review", "screen"])

    def w(**kw):
        return {"role": "review", "prompt": "審査して", "workdir_id": "wt-a", **kw}

    cases = [({"state": "ok"}, "ok", None), ({"state": "ok", "report": {"status": "dropped"}}, "skip", None),
             ({"state": "ok", "report": {"status": "failed"}}, "bug", "not_ready"),
             ({"state": "ok", "verify": {"rc": 3}}, "bug", "not_ready"),
             ({"state": "failed", "verify": {"rc": 1, "log_tail": "x"}}, "bug", "verify_failed"),
             ({"state": "failed", "verify": {"rc": 0}}, "error", "job_failed"),
             ({"state": "timed_out"}, "error", "job_timed_out"), ({"state": "lost"}, "error", "job_lost"), ({"state": "cancelled"}, "error", "job_cancelled")]
    for res, status, code in cases:
        env.pool.scripts["review"] = [res] * (env.svc.defaults.get("system_retries") + 1)
        st, o, _ = out(env, "worker.run", w(), man=man)
        assert st == status, (res, st)
        if code:
            assert o["error"]["code"] == code, (res, o)
        else:
            assert o["data"]["job_id"].startswith("job-") and o["data"]["model"] == "fake-review" and o["data"]["workdir_id"] == "wt-a"
    env.pool.scripts["review"] = [{"state": "timed_out"}] * (env.svc.defaults.get("system_retries") + 1)
    nrs = one(env, "worker.run", w(), man=man)[1]
    assert nrs[0]["out"]["error"]["class"] == "system.expected"             # 時間切れは再試行の対象


def test_worker_run_waits_for_a_running_job_and_submits_once(env):
    env.pool.pending_polls = 3
    st, o, _ = out(env, "worker.run", {"role": "screen", "prompt": "判定"}, man=manifest(roles=["screen"]))
    assert st == "ok" and len(env.pool.submitted) == 1 and env.pool.polls["job-1"] == 4


def test_worker_run_reads_prompt_files_and_refuses_paths_outside(env):
    st, o, _ = out(env, "worker.run", {"role": "screen", "prompt": "prompts/tdd-change/review.md"}, man=manifest(roles=["screen"]))
    assert st == "ok" and "審査（tdd-change の最後の段）" in env.pool.submitted[0]["prompt"]
    st, o, _ = out(env, "worker.run", {"role": "screen", "prompt": "../../../etc/passwd.md"}, man=manifest(roles=["screen"]))
    assert st == "error" and o["error"]["code"] == "prompt_missing"


def test_worker_run_rejected_by_the_pool_is_an_error(env):
    env.pool.reject_next = [(422, "bad_request")]
    st, o, _ = out(env, "worker.run", {"role": "screen", "prompt": "x"}, man=manifest(roles=["screen"]))
    assert st == "error" and o["error"]["code"] == "job_rejected" and o["error"]["class"] == "unknown"


def test_worker_run_adopts_the_job_of_an_interrupted_attempt(env):
    from tests.b7.test_t9_double_exec import Crash

    g = graph([node("n", "worker.run", {"role": "screen", "prompt": "判定"}, on_fail="eject")], [])
    ex = make_executor(env, g, manifest(roles=["screen"]))
    env.pool.pending_polls = 1
    orig = env.pool.job
    env.pool.job = lambda jid: (_ for _ in ()).throw(Crash())             # 待っている最中に落ちる

    async def first():
        await ex.run_once()
        import asyncio

        await asyncio.gather(*ex._tasks.values(), return_exceptions=True)

    run(first())
    assert len(env.pool.submitted) == 1 and ex.store.unfinished_node_runs()[0]["external_ref"] == {"job_id": "job-1"}
    env.pool.job = orig
    ex2 = make_executor(env, g, manifest(roles=["screen"]), store=ex.store, reset=False)

    async def second():
        await ex2.recover()
        await ex2.drain()

    run(second())
    assert len(env.pool.submitted) == 1                                    # 新しいジョブを出さず、前のジョブを引き継ぐ
    assert ex2.store.items()[0]["state"] == "completed" and ex2.store.node_runs(node_id="n")[-1]["external_ref"]["adopted"] is True


def test_decide_llm(env):
    sch = {"type": "object", "properties": {"route": {"enum": ["a", "b"]}}, "required": ["route"]}
    man = manifest(roles=["screen"])
    env.pool.scripts["screen"] = [{"report": {"status": "ready", "text": '考えた。{"route": "a"}'}}]
    st, o, _ = out(env, "decide.llm", {"prompt": "どっち", "schema": sch}, man=man)
    assert st == "ok" and o["data"]["decision"] == {"route": "a"} and sch["required"][0] in env.pool.submitted[0]["prompt"]
    assert env.pool.submitted[0]["role"] == "screen"
    env.pool.scripts["screen"] = [{"report": {"status": "ready", "text": '{"route": "z"}'}}]
    st, o, _ = out(env, "decide.llm", {"prompt": "どっち", "schema": sch}, man=man)
    assert st == "bug" and o["error"]["code"] == "decision_invalid"          # Schema に合わなければ bug
    env.pool.scripts["screen"] = [{"report": {"status": "ready", "text": "JSON なし"}}]
    assert out(env, "decide.llm", {"prompt": "どっち", "schema": sch}, man=man)[0] == "bug"


def test_verify_cmd_and_command_gates(env):
    base = {"workdir_id": "wt-a"}
    assert out(env, "verify.cmd", {**base, "cmd": ""})[0] == "skip" and not env.pool.submitted
    st, o, _ = out(env, "verify.cmd", {**base, "cmd": "pytest -q"})
    b = env.pool.submitted[0]
    assert st == "ok" and b["role"] == "verify" and b["verify"] == "pytest -q" and b["workdir_id"] == "wt-a" and b["sensitivity"] == "private"
    env.pool.scripts["verify"] = [{"state": "failed", "verify": {"rc": 1, "log_tail": "ng"}}]
    st, o, _ = out(env, "verify.cmd", {**base, "cmd": "pytest -q"})
    assert st == "bug" and o["data"]["rc"] == 1
    for typ in ("gate.lint", "gate.types"):
        env.pool.submitted.clear()
        assert out(env, typ, {**base, "cmd": None})[0] == "skip"
        assert out(env, typ, {**base, "cmd": "x"})[0] == "ok"
        env.pool.scripts["verify"] = [{"state": "failed", "verify": {"rc": 2, "log_tail": "ng"}}]
        st, o, _ = out(env, typ, {**base, "cmd": "x"})
        assert st == "bug" and o["error"]["code"] == typ.split(".")[1] + "_failed"
    assert out(env, "gate.mutation", {**base, "cmd": "m", "applies": None})[0] == "skip"
    assert out(env, "gate.mutation", {**base, "cmd": "m", "applies": "yes"})[0] == "ok"
    env.pool.scripts["verify"] = [{"state": "failed", "verify": {"rc": 2}}]
    assert out(env, "gate.mutation", {**base, "cmd": "m", "applies": True})[1]["error"]["code"] == "mutation_survived"
    env.pool.scripts["verify"] = [{"state": "timed_out"}] * (env.svc.defaults.get("system_retries") + 1)
    st, o, _ = out(env, "verify.cmd", {**base, "cmd": "x"})
    assert st == "error" and o["error"]["code"] == "job_timed_out"


def test_gate_coverage_changed(env):
    base = {"workdir_id": "wt-a", "cmd": "cov"}
    env.pool.scripts["verify"] = [{"verify": {"rc": 0, "log_tail": "changed: 79.9% total 90%"}}]
    st, o, _ = out(env, "gate.coverage_changed", base)
    assert st == "bug" and o["error"]["code"] == "coverage_low" and o["data"]["coverage"] == 0.799
    env.pool.scripts["verify"] = [{"verify": {"rc": 0, "log_tail": "diff coverage 80%"}}]
    assert out(env, "gate.coverage_changed", base)[0] == "ok"
    env.pool.scripts["verify"] = [{"verify": {"rc": 0, "log_tail": "diff coverage 79%"}}]
    st, o, _ = out(env, "gate.coverage_changed", base)
    assert st == "bug" and o["error"]["code"] == "coverage_low" and o["data"] == {"coverage": 0.79, "threshold": 0.8}
    env.pool.scripts["verify"] = [{"verify": {"rc": 0, "log_tail": "diff coverage 50%"}}]
    assert out(env, "gate.coverage_changed", {**base, "threshold": 0.5})[0] == "ok"            # with.threshold が優先
    for log in ("no numbers", "80% 90%", "changed: 120%", "changed: 80% diff coverage 90%"):
        env.pool.scripts["verify"] = [{"verify": {"rc": 0, "log_tail": log}}]
        assert out(env, "gate.coverage_changed", base)[1]["error"]["code"] == "coverage_unreadable", log
    env.pool.scripts["verify"] = [{"state": "failed", "verify": {"rc": 1, "log_tail": "changed: 99%"}}]
    st, o, _ = out(env, "gate.coverage_changed", base)
    assert st == "bug" and o["error"]["code"] == "coverage_command_failed"
    env.pool.scripts["verify"] = [{"verify": {"rc": 0, "log_tail": "80%"}}]
    assert out(env, "gate.coverage_changed", base)[0] == "ok"
    assert out(env, "gate.coverage_changed", {"workdir_id": "wt-a", "cmd": ""})[0] == "skip"


def test_gate_red_and_tests_unchanged_without_a_pool(env):
    d = env.work_root / "job-wt-a" / "tests" / "ax_generated" / "i"
    d.mkdir(parents=True)
    (d / "t.py").write_text("a", encoding="utf-8")
    st, o, _ = out(env, "gate.red", {"workdir_id": "wt-a", "tests_dir": "tests/ax_generated/i", "cmd": ""})
    assert st == "skip" and list(o["data"]["snapshot"]) == ["tests/ax_generated/i/t.py"]
    st, o, _ = out(env, "gate.red", {"workdir_id": "wt-a", "tests_dir": "tests/ax_generated/none", "cmd": "x"})
    assert st == "bug" and o["error"]["code"] == "no_tests"
    snap = nodelib.snapshot_tree(env.work_root / "job-wt-a", "tests/ax_generated/i")
    assert out(env, "gate.tests_unchanged", {"workdir_id": "wt-a", "tests_dir": "tests/ax_generated/i", "snapshot": snap})[0] == "ok"
    assert out(env, "gate.tests_unchanged", {"workdir_id": "wt-a", "tests_dir": "tests/ax_generated/i", "snapshot": None})[1]["error"]["code"] == "snapshot_missing"
    # パスの外へ出る指定は空の記録になる
    assert nodelib.snapshot_tree(env.work_root / "job-wt-a", "../..") == {}


def test_gate_secret_scan(env):
    wd = env.work_root / "job-wt-a"
    wd.mkdir()
    secret = "gh" + "p_" + "A1b2C3d4" * 5                     # 試験のソースに秘密の形の実値を書かない（実行時に連結する）
    (wd / "clean.txt").write_text("ふつうの文\n", encoding="utf-8")
    (wd / "leak.txt").write_text(f"token={secret}\n", encoding="utf-8")
    st, o, _ = out(env, "gate.secret_scan", {"texts": ["きれいな文"], "workdir_id": "wt-a", "paths": ["clean.txt"]})
    assert st == "ok" and o["data"] == {"checked_texts": 1, "checked_files": 1}
    st, o, _ = out(env, "gate.secret_scan", {"texts": ["x", f"漏れ {secret}"], "workdir_id": "wt-a", "paths": ["leak.txt"]})
    assert st == "bug" and o["error"]["code"] == "secret_found"
    wheres = sorted({f["where"] for f in o["data"]["findings"]})
    assert wheres == ["leak.txt", "texts[1]"]
    assert secret not in json.dumps(o) and secret not in json.dumps(env.sink.events)       # 値は出力にも事象にも入れない
    st, o, _ = out(env, "gate.secret_scan", {"workdir_id": "wt-a", "paths": ["../outside"]})
    assert o["error"]["code"] == "path_outside"
    assert out(env, "gate.secret_scan", {"paths": ["x"]})[1]["error"]["code"] == "workdir_required"


# ---- ledger / wait / notify / env ----
def test_ledger_add_is_idempotent_per_item_and_node(env):
    it, nrs, ex = one(env, "ledger.add", {"title": "確認する", "state": "parked", "reason": "x", "check_at": "2026-11-06T00:00:00.000Z", "owner": "system"})
    assert nrs[0]["status"] == "ok" and len(env.ledger.created) == 1
    t = env.ledger.created[0]
    assert t["subloop"] == "work-tdd" and t["source"]["item_id"] == it["item_id"] and t["id"] == nrs[0]["out"]["data"]["todo_id"]

    async def again():
        ex.store.set_item_state(it["item_id"], "planned")
        await ex._pump()
        await ex.drain()

    run(again())
    assert len(env.ledger.created) == 1 and ex.store.node_runs(node_id="n")[-1]["out"]["data"]["reused"] is True


def test_ledger_wait_and_wait_event_return_outcomes(env, fake_clock):
    env.ledger.todos["t1"] = {"id": "t1", "state": "running"}
    st, o, _ = out(env, "ledger.wait", {"todo_id": "t1", "until": ["done", "review"], "wait_s": 10, "poll_s": 2})
    assert st == "ok" and o["data"]["outcome"] == "timeout" and fake_clock.sleeps                       # 期限までに届かなければ timeout
    env.ledger.todos["t1"]["state"] = "review"
    st, o, _ = out(env, "ledger.wait", {"todo_id": "t1", "until": ["done", "review"], "wait_s": 10})
    assert o["data"] == {"state": "review", "outcome": "review"}
    assert out(env, "ledger.wait", {"todo_id": "zz", "until": ["done"]})[1]["error"]["code"] == "todo_not_found"
    st, o, _ = out(env, "wait.event", {"kind": "task.state", "wait_s": 3, "poll_s": 1})
    assert o["data"]["outcome"] == "timeout"
    env.timeline.add({"kind": "task.state", "subject": "t1", "id": "E1"})
    st, o, _ = out(env, "wait.event", {"kind": "task.state", "subject": "t1", "wait_s": 3})
    assert o["data"]["outcome"] == "event" and o["data"]["event"]["id"] == "E1"
    env.timeline.down = True
    assert out(env, "wait.event", {"kind": "task.state", "wait_s": 2, "poll_s": 1})[1]["data"]["outcome"] == "timeout"       # 時系列記録に届かなくても止めない


def test_notify_writes_a_deterministic_notification_event(env):
    st, o, it = out(env, "notify", {"title": "終わった", "text": "ブランチを作った"})
    ev = env.sink.of("change.notification")
    assert st == "ok" and len(ev) == 1 and ev[0]["data"]["notification_id"] == o["data"]["notification_id"] and ev[0]["data"]["state"] == "requested"
    assert ev[0]["data"]["item_id"] == it["item_id"] and ev[0]["data"]["title"] == "終わった"


def test_env_run_cancel_and_reconcile(env):
    req = {"requires": ["hibi-state-read"], "sensitivity": "private", "entry": {"kind": "entrypoint", "name": "hibi-state", "args": {}}, "outputs": ["state.json"],
           "budget_min": 2}
    st, o, it = out(env, "env.run", dict(req, job_id="job-aaa"))
    assert st == "ok" and o["data"]["state"] == "succeeded" and o["data"]["artifacts"][0]["name"] == "state.json" and env.resources.posted[0]["job_id"] == "job-aaa"
    assert env.resources.posted[0]["scope"] == "one"
    env.resources.state = "failed"
    assert out(env, "env.run", dict(req, job_id="job-bbb"))[0] == "bug"
    env.resources.state = "timed_out"
    st, o, _ = out(env, "env.run", dict(req, job_id="job-ccc"))
    assert st == "error" and o["error"]["code"] == "env_job_timed_out"
    env.resources.state = "succeeded"
    # 同じ job_id は受付済み(409)として続ける。irreversible は launching を書く
    st, o, _ = out(env, "env.run", dict(req, job_id="job-aaa", irreversible=True))
    assert st == "ok" and any(e["data"].get("key") == "env-run:job-aaa" for e in env.sink.of("node.state", state="launching"))
    assert out(env, "env.cancel", {"job_id": "job-aaa"})[0] == "ok" and env.resources.cancelled == ["job-aaa"]
    assert out(env, "env.cancel", {"job_id": "job-nope"})[1]["data"]["already_ended"] is True
    env.svc.resources = None
    assert out(env, "env.run", req)[1]["error"]["code"] == "resources_unavailable"


def test_env_run_reconcile_verdicts(env):
    from runtime.subloop.ctx import NodeContext
    from runtime.subloop.nodes import load_registry

    reg = load_registry(ROOT / "plugins" / "nodes")
    t = reg.get("env.run")
    ex = make_executor(env, graph([node("n", "env.run", {"requires": [], "sensitivity": "public", "entry": {}})], []))

    async def verdict(jobs=None, boom=None):
        env.resources.jobs = jobs or {}
        if boom:
            env.resources.get = lambda *a, **k: (_ for _ in ()).throw(boom)
        ctx = NodeContext(ex, item={"item_id": "i"}, node=ex.graph.nodes["n"], run_id="r", nr_id=0, attempt=1, continuation=0, deadline="2026-11-05T13:00:00.000Z")
        return await t.reconcile(ctx, "env-run:job-x")

    assert run(verdict()) == {"found": False}                                           # 無い → 未実行(再実行してよい)
    v = run(verdict({"job-x": {"job_id": "job-x", "state": "succeeded"}}))
    assert v["found"] is True and v["out"]["status"] == "ok"
    v = run(verdict({"job-x": {"job_id": "job-x", "state": "running"}}))
    assert v["found"] is True and v["out"]["status"] == "error" and v["out"]["error"]["class"] == "system.expected"
    assert run(verdict(boom=Unavailable("x"))) is None

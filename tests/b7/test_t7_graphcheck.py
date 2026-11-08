"""T-7 グラフ検査（S-7.7、D-6.4）。上限のない循環、たどり着けないノード、失敗時の行き先がないノード、timeout_s の無いノード、宣言のない knob、
秘密検査なしの公開、tdd-change を通らない実装を拒否する。"""
import copy
import json
from pathlib import Path

import pytest

from runtime.subloop import graphcheck
from runtime.subloop.graph import GraphError, expand, load_yaml
from runtime.subloop.nodes import load_registry
from tests.b7.helpers import ROOT, load_tool

LIB = ROOT / "plugins" / "subloop" / "_lib"
REG = load_registry(ROOT / "plugins" / "nodes")


def man(**over):
    m = {"schema_version": 1, "id": "x", "version": "1.0.0", "kind": "subloop", "title": "t", "image": "i", "execution": "llm",
         "schedule": {"kind": "interval", "every_s": 60}, "capabilities": ["run", "draining"], "health": {"heartbeat_s": 60}, "sensitivity": "private",
         "roles": ["test_design", "impl", "review", "assist"], "outputs": [], "knobs": {"repo": {"type": "string", "default": "o/r", "effective": "next_run", "title": "r"}}}
    m.update(over)
    return m


def codes(doc, manifest=None, reg=REG):
    return sorted({f.code for f in graphcheck.check_graph(doc, manifest or man(), lib_dir=LIB, registry=reg) if f.severity == "error"})


def n(id_, typ, with_=None, **kw):
    d = {"id": id_, "type": typ, "with": with_ or {}, "limits": {"timeout_s": 60}}
    d.update(kw)
    return d


def g(nodes, edges, entry=None):
    return {"schema_version": 1, "entry": entry or nodes[0].get("id") or nodes[0].get("as") or nodes[0]["include"], "nodes": nodes, "edges": edges}


SH = {"cmd": "true"}


def test_a_valid_graph_passes():
    doc = g([n("a", "shell.run", SH, on_fail="eject"), n("b", "shell.run", SH, on_fail="eject")], [{"from": "a", "to": "b"}])
    assert codes(doc) == []


def test_loop_without_max_loops_is_rejected_and_with_it_accepted():
    bad = g([n("a", "shell.run", SH, on_fail="eject"), n("b", "shell.run", SH, on_fail="eject")],
            [{"from": "a", "to": "b"}, {"from": "b", "to": "a", "when": "bug"}])
    assert codes(bad) == ["loop_without_max_loops"]
    good = copy.deepcopy(bad)
    good["edges"][1]["max_loops"] = 2
    assert codes(good) == []
    selfloop = g([n("a", "shell.run", SH, on_fail="eject")], [{"from": "a", "to": "a", "when": "bug"}])
    assert codes(selfloop) == ["loop_without_max_loops"]
    selfloop["edges"][0]["max_loops"] = 1
    assert codes(selfloop) == []


def test_goto_that_makes_a_cycle_is_rejected():
    doc = g([n("a", "shell.run", SH, on_fail="goto:b"), n("b", "shell.run", SH, on_fail="goto:a")], [{"from": "a", "to": "b"}])
    assert "loop_without_max_loops" in codes(doc)


def test_unreachable_node_is_rejected():
    doc = g([n("a", "shell.run", SH, on_fail="eject"), n("island", "shell.run", SH, on_fail="eject")], [])
    assert codes(doc) == ["unreachable"]


def test_node_without_failure_route_is_rejected_only_when_the_type_can_bug():
    assert codes(g([n("a", "shell.run", SH)], [])) == ["no_failure_route"]
    assert codes(g([n("a", "shell.run", SH)], [{"from": "a", "to": "a", "when": "bug", "max_loops": 1}])) == []   # bug の辺が失敗時の行き先
    ok_doc = g([n("p", "git.push", {"remote": "work.git", "branch": "ai/x", "workdir_id": "wt-a"})], [])           # push は bug を返さない（S-7.10 の例）
    assert codes(ok_doc, man(outputs=["git.push"])) == []


def test_node_without_timeout_s_is_rejected_by_the_schema():
    doc = g([{"id": "a", "type": "shell.run", "with": SH, "limits": {}, "on_fail": "eject"}], [])
    assert codes(doc) == ["graph_schema"]
    doc = g([{"id": "a", "type": "shell.run", "with": SH, "on_fail": "eject"}], [])
    assert codes(doc) == ["graph_schema"]


def test_knob_must_be_declared_in_the_manifest():
    doc = g([n("a", "shell.run", {"cmd": "{{knobs.undeclared}}"}, on_fail="eject")], [])
    assert codes(doc) == ["knob_undeclared"]
    doc = g([n("a", "shell.run", {"cmd": "{{knobs.repo}}"}, on_fail="eject")], [])
    assert codes(doc) == []


def test_external_effect_type_must_be_in_manifest_outputs():
    cm = n("c", "gh.comment", {"repo": "o/r", "number": 1, "body": "x"})
    sc = n("s", "gate.secret_scan", {"texts": ["x"]}, on_fail="eject")
    doc = g([sc, cm], [{"from": "s", "to": "c"}])
    assert codes(doc) == ["output_undeclared"]
    assert codes(doc, man(outputs=["github.comment"])) == []


def test_publishing_without_secret_scan_on_every_path_is_rejected():
    cm = n("c", "gh.comment", {"repo": "o/r", "number": 1, "body": "x"})
    st = n("st", "shell.run", SH, on_fail="eject")
    sc = n("s", "gate.secret_scan", {"texts": ["x"]}, on_fail="eject")
    m = man(outputs=["github.comment"])
    no_scan = g([st, cm], [{"from": "st", "to": "c"}])
    assert codes(no_scan, m) == ["publish_without_secret_scan"]
    # 2 つの経路の片方だけ scan を通る → 外れる（経路を逆にたどって確かめる）
    two = g([n("st", "shell.run", SH), sc, cm], [{"from": "st", "to": "s", "when": "ok"}, {"from": "st", "to": "c", "when": "skip"}, {"from": "s", "to": "c"}])
    two["nodes"][0]["on_fail"] = "eject"
    assert codes(two, m) == ["publish_without_secret_scan"]
    both = g([n("st", "shell.run", SH, on_fail="eject"), sc, cm], [{"from": "st", "to": "s"}, {"from": "s", "to": "c"}])
    assert codes(both, m) == []


def test_implementation_must_go_through_tdd_change():
    impl = n("i", "worker.run", {"role": "impl", "prompt": "x", "workdir_id": "wt-a"}, on_fail="eject")
    assert codes(g([impl], [])) == ["impl_outside_tdd_change"]
    hard = n("i", "worker.run", {"role": "impl_hard", "prompt": "x", "workdir_id": "wt-a"}, on_fail="eject")
    assert "impl_outside_tdd_change" in codes(g([hard], []), man(roles=["impl_hard"]))
    # ロールが実行時に決まる（差し込み）でも、実装を含みうる manifest なら tdd-change の中でなければならない
    dyn = n("i", "worker.run", {"role": "{{item.role}}", "prompt": "x", "workdir_id": "wt-a"}, on_fail="eject")
    assert "impl_outside_tdd_change" in codes(g([dyn], []))
    doc = g([{"include": "tdd-change", "as": "tdd-change"}], [])
    assert codes(doc, man(knobs={"max_hops": {"type": "integer", "default": 20, "effective": "next_run", "title": "x"}})) == []
    # ID が tdd-change. で始まらない(別名で取り込んだ)実装は、判定の対象外ではなく外れる
    renamed = g([{"include": "tdd-change", "as": "mine"}], [])
    assert "impl_outside_tdd_change" in codes(renamed, man(knobs={"max_hops": {"type": "integer", "default": 20, "effective": "next_run", "title": "x"}}))


def test_with_schema_and_unknown_type():
    assert codes(g([n("a", "shell.run", {"cmd": ""}, on_fail="eject")], [])) == ["with_schema"]
    assert codes(g([n("a", "shell.run", {"cmd": "x", "bogus": 1}, on_fail="eject")], [])) == ["with_schema"]
    assert codes(g([n("a", "nope.nothing", {}, on_fail="eject")], [])) == ["unknown_type"]
    # 差し込みの値は型を確かめられない（実行時に決まる）
    assert codes(g([n("a", "gh.list_issues", {"repo": "o/r", "limit": "{{knobs.repo}}"}, on_fail="eject")], [])) == []

def test_declared_external_item_source_requires_item_acceptance():
    doc = g([n("a", "shell.run", SH, on_fail="eject")], [])
    doc["item_source"] = "release.pending"
    assert codes(doc, man(accepts_items=True)) == ["item_source_unknown"]
    assert codes(doc, man(accepts_items=True, outputs=["release.pending"])) == []
    assert codes(doc, man(accepts_items=False, outputs=["release.pending"])) == ["item_source_unknown"]


def test_role_and_sensitivity_checks():
    wr = lambda role, **w: g([n("a", "worker.run", {"role": role, "prompt": "p", **w}, on_fail="eject")], [])  # noqa: E731
    assert "role_undeclared" in codes(wr("summary"))
    assert codes(wr("assist"), man(roles=["assist"])) == []
    assert "sensitivity_conflict" in codes(wr("assist"), man(roles=["assist"], sensitivity="sensitive"))
    assert codes(wr("review", workdir_id="wt-a"), man(roles=["review"], sensitivity="sensitive")) == []
    # 読み取り専用の役割がリポジトリの worktree を渡されて書き換えようとする → 外れる（書き換えは work-tdd だけ。T-11）
    assert "readonly_role_workdir" in codes(wr("assist", workdir_id="wt-a"), man(roles=["assist"]))
    assert "role_unknown" in codes(wr("nobody"))


def test_outcome_edge_must_be_declared_by_the_type():
    wait = n("w", "ledger.wait", {"todo_id": "t1", "until": ["done"], "wait_s": 5})
    ok_doc = g([wait, n("b", "shell.run", SH, on_fail="eject")], [{"from": "w", "to": "b", "when": "outcome:timeout"}])
    assert codes(ok_doc) == []
    bad = g([wait, n("b", "shell.run", SH, on_fail="eject")], [{"from": "w", "to": "b", "when": "outcome:nope"}])
    assert codes(bad) == ["outcome_undeclared"]


def test_required_hops_must_fit_the_manifest_limit():
    k = lambda v: {"max_hops": {"type": "integer", "default": v, "effective": "next_run", "title": "x"}}  # noqa: E731
    doc = g([{"include": "tdd-change", "as": "tdd-change"}], [])
    assert graphcheck.required_hops(expand(doc, LIB)) == 20
    assert codes(doc, man(knobs=k(20))) == []
    assert codes(doc, man(knobs=k(10))) == ["hops_exceed"]
    assert codes(doc, man(knobs={})) == ["hops_exceed"]          # 既定 8（S-17）では足りない


def test_include_rules(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()

    def put(name, **body):
        base = {"schema_version": 1, "params": {}, "entry": "a", "exits": {"ok": "a"}, "nodes": [n("a", "shell.run", SH, on_fail="eject")], "edges": []}
        base.update(body)
        import yaml

        (lib / f"{name}.yaml").write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")

    put("leaf", params={"cmd": "echo", "must": {"required": True}}, nodes=[n("a", "shell.run", {"cmd": "{{params.cmd}}"}, on_fail="eject")])
    with pytest.raises(GraphError) as e:
        expand(g([{"include": "leaf", "as": "x"}], []), lib)
    assert e.value.code == "include_param"                         # 必須のパラメータが無い
    with pytest.raises(GraphError) as e:
        expand(g([{"include": "leaf", "as": "x", "with": {"must": 1, "bogus": 2}}], []), lib)
    assert e.value.code == "include_param"                         # 無いパラメータ
    ge = expand(g([{"include": "leaf", "as": "x", "with": {"must": 1, "cmd": "{{item.c}}"}}, n("z", "shell.run", SH, on_fail="eject")],
                  [{"from": "x", "to": "z"}]), lib)
    assert ge.entry == "x.a" and ge.nodes["x.a"].with_ == {"cmd": "{{item.c}}"} and [(e.src, e.dst, e.when) for e in ge.edges] == [("x.a", "z", "ok")]
    with pytest.raises(GraphError) as e:
        expand(g([{"include": "nope", "as": "x"}], []), lib)
    assert e.value.code == "include_unknown"
    put("selfinc", nodes=[{"include": "selfinc", "as": "i"}])
    with pytest.raises(GraphError) as e:
        expand(g([{"include": "selfinc"}], []), lib)
    assert e.value.code == "include_cycle"
    # 深さは 3 まで
    put("d4")
    put("d3", nodes=[{"include": "d4", "as": "n"}], entry="n", exits={"ok": "n.a"})
    put("d2", nodes=[{"include": "d3", "as": "n"}], entry="n", exits={"ok": "n.n.a"})
    put("d1", nodes=[{"include": "d2", "as": "n"}], entry="n", exits={"ok": "n.n.n.a"})
    with pytest.raises(GraphError):
        expand(g([{"include": "d1", "as": "top"}], []), lib)       # top → d1 → d2 → d3 → d4 は深さ 4
    expand(g([{"include": "d2", "as": "top"}], []), lib)           # top → d2 → d3 → d4 は深さ 3


def test_include_renames_node_references_and_goto(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    import yaml

    sub = {"schema_version": 1, "params": {}, "entry": "a", "exits": {"ok": "b"},
           "nodes": [n("a", "shell.run", SH, on_fail="goto:b"), n("b", "shell.run", {"cmd": "{{nodes.a.data.rc}}"}, on_fail="eject")], "edges": [{"from": "a", "to": "b"}]}
    (lib / "pair.yaml").write_text(yaml.safe_dump(sub), encoding="utf-8")
    ge = expand(g([{"include": "pair", "as": "p"}, n("after", "shell.run", {"cmd": "{{nodes.p.b.data.rc}}"}, on_fail="eject")], [{"from": "p", "to": "after", "when": "ok"}]), lib)
    assert ge.nodes["p.b"].with_ == {"cmd": "{{nodes.p.a.data.rc}}"} and ge.nodes["p.a"].on_fail == "goto:p.b"
    assert [(e.src, e.dst) for e in ge.edges] == [("p.a", "p.b"), ("p.b", "after")]       # from: p / when: ok は exits.ok のノードに結ばれる


def test_max_loops_and_hops_are_counted_on_the_expanded_graph():
    ge = expand(load_yaml(ROOT / "plugins" / "subloop" / "work-tdd" / "graph.yaml"), LIB)
    assert all(nid.startswith("tdd-change.") or nid in ("take", "push") for nid in ge.nodes)
    loops = [e for e in ge.edges if e.max_loops]
    assert len(loops) == 7 and sum(e.max_loops for e in loops) == 20


def test_subloop_create_bounds_s77_12():
    m = {"secrets": ["github-token"], "outputs": ["git.push"], "sensitivity": "private"}
    req = {"secrets": ["github-token", "x"], "outputs": ["git.push", "github.pr"], "sensitivity": "private"}
    assert graphcheck.check_request_bounds(m, req) == []
    assert [f.code for f in graphcheck.check_request_bounds(m, {**req, "secrets": []})] == ["secrets_exceed_request"]
    assert [f.code for f in graphcheck.check_request_bounds(m, {**req, "outputs": []})] == ["outputs_exceed_request"]
    assert [f.code for f in graphcheck.check_request_bounds(m, {**req, "sensitivity": "public"})] == ["sensitivity_exceeds_request"]
    # 司令塔の受け口から operate で受けた依頼は、さらに secrets が空、outputs が空、sensitivity が public
    assert len(graphcheck.check_request_bounds(m, req, via="operate")) == 3
    assert graphcheck.check_request_bounds({"secrets": [], "outputs": [], "sensitivity": "public"}, req, via="operate") == []


def test_manifest_checks():
    base = load_yaml(ROOT / "plugins" / "subloop" / "work-tdd" / "manifest.yaml")
    assert graphcheck.check_manifest(base) == []
    bad = copy.deepcopy(base)
    bad["knobs"]["max_parallel_items"]["default"] = 5
    assert [f.code for f in graphcheck.check_manifest(bad)] == ["knob_default_out_of_range"]
    bad = copy.deepcopy(base)
    del bad["knobs"]["inbox_max"]
    assert [f.code for f in graphcheck.check_manifest(bad)] == ["inbox_max_missing"]
    bad = copy.deepcopy(base)
    bad["capabilities"] = ["run"]
    assert graphcheck.check_manifest(bad)[0].code == "manifest_schema"                         # draining は必須
    assert graphcheck.check_manifest(load_yaml(ROOT / "templates" / "subloop" / "manifest.yaml")) == []


def test_validate_body_is_what_the_control_api_returns():
    import jsonschema

    schema = json.loads((ROOT / "schemas" / "ext" / "b7" / "validate-response.schema.json").read_text(encoding="utf-8"))
    good = graphcheck.validate_body("graph", {"graph": load_yaml(ROOT / "plugins/subloop/work-tdd/graph.yaml"), "manifest": load_yaml(ROOT / "plugins/subloop/work-tdd/manifest.yaml")}, root=ROOT)
    assert good["ok"] is True and good["errors"] == []
    bad = graphcheck.validate_body("graph", g([n("a", "shell.run", SH)], []), root=ROOT)
    assert bad["ok"] is False and bad["errors"][0]["code"] == "no_failure_route"
    m = graphcheck.validate_body("manifest", {"id": "x"}, root=ROOT)
    assert m["ok"] is False
    assert graphcheck.validate_body("bogus", {}, root=ROOT)["ok"] is False
    for r in (good, bad, m):
        jsonschema.validate(r, schema)


def test_check_subloop_dir_reads_declared_node_dirs(tmp_path):
    import yaml as _yaml

    sub = tmp_path / "mine"
    (sub / "integration" / "nodes" / "ext").mkdir(parents=True)
    (sub / "integration" / "nodes" / "ext" / "hook.py").write_text(
        "schema = {'type': 'object'}\nasync def run(c, i):\n    from runtime.subloop.nodeapi import ok\n    return ok()\n", encoding="utf-8")
    (sub / "manifest.yaml").write_text(_yaml.safe_dump({**man(), "id": "mine", "graph": "graph.yaml"}), encoding="utf-8")
    doc = g([n("a", "ext.hook", {}, on_fail="eject")], [])
    (sub / "graph.yaml").write_text(_yaml.safe_dump(doc), encoding="utf-8")
    # 宣言が無ければ型が不明で外れる。nodes.yaml を置く（宣言する）と通る。
    no_decl = graphcheck.check_subloop_dir(sub, root=ROOT)
    assert "unknown_type" in {f.code for f in no_decl}
    (sub / "nodes.yaml").write_text("node_dirs: [integration/nodes]\n", encoding="utf-8")
    ok = graphcheck.check_subloop_dir(sub, root=ROOT)
    assert ok == [], [str(f) for f in ok]
    # 渡された登録簿にも宣言の型を足す（実行時の register 済みの型を壊さない）。
    ok2 = graphcheck.check_subloop_dir(sub, root=ROOT, registry=load_registry(ROOT / "plugins" / "nodes"))
    assert ok2 == [], [str(f) for f in ok2]


def test_real_plugins_and_templates_pass_the_cli(tmp_path, capsys):
    tool = load_tool("ax_graph_check")
    subloops = tmp_path / "subloops"
    subloops.mkdir()
    for name in ("work-tdd", "change", "release", "_lib"):
        (subloops / name).symlink_to(ROOT / "plugins" / "subloop" / name, target_is_directory=True)
    rc = tool.main(["--root", str(ROOT), str(subloops), str(ROOT / "templates")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert all(name in out for name in ("work-tdd", "change", "release", "tdd-change.yaml", "templates"))


def test_cli_exit_codes_and_json(tmp_path, capsys):
    import yaml

    tool = load_tool("ax_graph_check")
    d = tmp_path / "plugins" / "subloop" / "bad"
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump({**man(), "id": "bad", "graph": "graph.yaml"}), encoding="utf-8")
    (d / "graph.yaml").write_text(yaml.safe_dump(g([n("a", "worker.run", {"role": "impl", "prompt": "p", "workdir_id": "wt-a"}, on_fail="eject")], [])), encoding="utf-8")
    assert tool.main(["--root", str(ROOT), str(tmp_path / "plugins" / "subloop")]) == 1
    assert "impl_outside_tdd_change" in capsys.readouterr().out
    assert tool.main(["--root", str(ROOT), "--json", str(d)]) == 1
    j = json.loads(capsys.readouterr().out)
    assert j["ok"] is False and j["results"][0]["findings"][0]["code"] == "impl_outside_tdd_change"
    assert tool.main(["--root", str(ROOT), str(tmp_path / "nope")]) == 2

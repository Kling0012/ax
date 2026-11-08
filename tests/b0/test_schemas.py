"""schemas/ の検査（B0。S-2.1〜S-17 と各 S の API・事象のスキーマ）。"""
import json

import pytest
import yaml

from axcommon import schemas

ULID = "01J00000000000000000000000"
TS = "2026-11-05T12:00:00.000Z"


def test_all_schemas_pass_metaschema_and_refs_resolve():
    assert schemas.check_all() == []


def test_every_schema_file_is_listed_and_has_expected_id():
    names = schemas.all_names()
    assert len(names) >= 30
    for n in names:
        assert schemas.load(n)["$id"] == schemas.BASE_URI + n


@pytest.mark.parametrize("name", ["graph", "work-item", "hibi-state", "runner", "manifest", "event", "todo", "control-command"])
def test_schema_files_named_in_documents_exist(name):
    # SPEC S-7.1 graph.schema.json、S-7.10 work-item.schema.json、S-23.3 hibi-state.schema.json、FACTS F-7.2 runner.schema.json
    assert schemas.load(name)["title"]


def _ev(kind="run.state", data=None):
    return {"id": ULID, "ts": TS, "source": {"component": "issue-automation", "host": "hub-host"}, "kind": kind, "subject": "s", "data": data or {}}


def test_event_minimum_fields_ok():
    assert schemas.errors("event", _ev(data={"state": "running"})) == []


def test_event_missing_required_field_rejected():
    ev = _ev(data={"state": "running"})
    del ev["subject"]
    assert any("subject" in e for e in schemas.errors("event", ev))


def test_event_run_state_requires_known_state():
    assert schemas.errors("event", _ev(data={"state": "bogus"}))
    assert schemas.errors("event", _ev(data={}))  # 投影が読む data.state が無い


def test_event_job_state_requires_job_id_and_state():
    assert schemas.errors("event", _ev("job.state", {"state": "running"}))
    assert schemas.errors("event", _ev("job.state", {"job_id": "j1", "state": "running", "model": "m", "role": "impl"})) == []


def test_event_unknown_kind_has_free_data():
    assert schemas.errors("event", _ev("worker.selection", {"anything": [1, 2]})) == []


def test_event_bad_ulid_and_ts_rejected():
    ev = _ev(data={"state": "running"})
    ev["id"] = "not-a-ulid"
    ev["ts"] = "2026-11-05 12:00:00"
    assert len(schemas.errors("event", ev)) >= 2


def test_control_command_example_from_spec_ok():
    cmd = {"id": ULID, "target": "issue-automation", "op": "set_mode", "args": {"mode": "collect_only"}, "reason": "点検の30分前",
           "issued_by": "manager", "issued_at": TS, "expires_at": "2026-11-05T13:00:00.000Z", "on_expiry": "ask_issuer", "corr": ULID}
    assert schemas.errors("control-command", cmd) == []


def test_control_command_non_user_requires_expiry_but_user_may_omit():
    base = {"id": ULID, "target": "x", "op": "run_now", "args": {}, "reason": "r", "issued_at": TS}
    assert schemas.errors("control-command", {**base, "issued_by": "commander"})
    assert schemas.errors("control-command", {**base, "issued_by": "user"}) == []


@pytest.mark.parametrize("op,args,ok", [
    ("set_mode", {"mode": "draining"}, True), ("set_mode", {"mode": "pause"}, False),
    ("set_knob", {"name": "every_s", "value": 120}, True), ("set_knob", {"name": "every_s"}, False),
    ("exclude_resource", {"resource_id": "m1", "until": "2026-11-05T13:00:00Z"}, True), ("exclude_resource", {"resource_id": "m1"}, False),
    ("cancel", {"run_id": ULID}, True), ("cancel", {}, False), ("run_now", {}, True), ("run_now", {"x": 1}, False),
])
def test_control_command_args_checked_per_op(op, args, ok):
    cmd = {"id": ULID, "target": "t", "op": op, "args": args, "reason": "r", "issued_by": "user", "issued_at": TS}
    assert (schemas.errors("control-command", cmd) == []) is ok


def test_manifest_example_from_spec_ok():
    m = yaml.safe_load("""
schema_version: 1
id: issue-automation
version: 1.0.0
kind: subloop
project: dreamforge
title: GitHub の Issue を実装する
image: ax/runtime:1.0.0
execution: mechanical
schedule: {kind: interval, every_s: 300, missed: record_only}
graph: graph.yaml
capabilities: [run, collect_only, poll_paused, draining]
knobs:
  every_s: {type: integer, default: 300, min: 60, max: 3600, effective: next_run, title: 巡回の間隔（秒）}
health: {heartbeat_s: 60}
sensitivity: private
roles: [test_design, impl, review]
secrets: [github-token]
outputs: [github.pr, github.comment]
depends_on_plugins: [worker-pool]
""")
    assert schemas.errors("manifest", m) == []


def test_manifest_capabilities_must_include_run_and_draining():
    m = {"schema_version": 1, "id": "p", "version": "1.0.0", "kind": "resource", "title": "t", "image": "i", "execution": "mechanical",
         "schedule": {"kind": "interval", "every_s": 60}, "capabilities": ["run"], "health": {"heartbeat_s": 60}, "sensitivity": "public"}
    assert schemas.errors("manifest", m)
    m["capabilities"] = ["run", "draining"]
    assert schemas.errors("manifest", m) == []


def test_manifest_subloop_requires_graph_and_accepts_items_only_for_subloop():
    m = {"schema_version": 1, "id": "p", "version": "1.0.0", "kind": "subloop", "title": "t", "image": "i", "execution": "llm",
         "schedule": {"kind": "event"}, "capabilities": ["run", "draining"], "health": {"heartbeat_s": 60}, "sensitivity": "public"}
    assert schemas.errors("manifest", m)  # graph が無い
    m["graph"] = "graph.yaml"
    assert schemas.errors("manifest", m) == []
    m2 = {**m, "kind": "timer", "accepts_items": True}
    del m2["graph"]
    assert schemas.errors("manifest", m2)


def test_graph_example_from_spec_ok_and_unbounded_edge_fields_rejected():
    g = yaml.safe_load("""
schema_version: 1
entry: list
item_source: list
max_parallel_items: 2
nodes:
  - id: list
    type: gh.list_issues
    with: {repo: "{{knobs.repo}}", label: ai-ready}
    limits: {timeout_s: 120, retries: 2}
  - id: design
    type: worker.run
    with: {role: test_design, prompt: prompts/test_design.md}
    limits: {timeout_s: 3600, retries: 1, continuations: 2}
    on_fail: eject
edges:
  - {from: design, to: list, when: ok}
  - {from: design, to: design, when: bug, max_loops: 3}
""")
    assert schemas.errors("graph", g) == []
    g["edges"][1]["max_loops"] = 0
    assert schemas.errors("graph", g)
    g["edges"][1]["when"] = "weird"
    assert schemas.errors("graph", g)


def test_graph_node_requires_timeout():
    g = {"schema_version": 1, "entry": "a", "nodes": [{"id": "a", "type": "x", "limits": {}}], "edges": []}
    assert schemas.errors("graph", g)


def test_work_item_requires_both_acceptance_lists():
    item = {"item_key": "k", "todo_id": "t", "kind": "work", "project": "p", "role": "impl", "title": "題", "body": "本文",
            "acceptance": {"examples": ["a"], "boundaries": ["b"]}}
    assert schemas.errors("work-item", item) == []
    item["acceptance"] = {"examples": ["a"]}
    assert schemas.errors("work-item", item)
    item["acceptance"] = {"examples": ["a"], "boundaries": []}
    assert schemas.errors("work-item", item)


def test_work_item_subloop_create_needs_template():
    item = {"item_key": "k", "todo_id": "t", "kind": "subloop_create", "project": "p", "role": "impl", "title": "題", "body": "b",
            "acceptance": {"examples": ["a"], "boundaries": ["b"]}}
    assert schemas.errors("work-item", item)
    item["template"] = "basic"
    assert schemas.errors("work-item", item) == []


def test_todo_parked_needs_reason_and_check_at_waiting_user_needs_card():
    base = {"title": "題", "owner": "system"}
    assert schemas.errors("todo", {**base, "state": "parked"})
    assert schemas.errors("todo", {**base, "state": "parked", "reason": "x", "check_at": TS}) == []
    assert schemas.errors("todo", {**base, "state": "waiting_user"})
    assert schemas.errors("todo", {**base, "state": "waiting_user", "card": {"question": "q", "waiting_reason": "preference"}}) == []
    assert schemas.errors("todo", {**base, "state": "waiting_user", "card": {"question": "q", "waiting_reason": "tired"}})


def test_todo_answer_relay_needs_quote():
    assert schemas.errors("todo-answer", {"answer": "はい", "answer_source": "relay"})
    assert schemas.errors("todo-answer", {"answer": "はい", "answer_source": "relay", "quote": "はい"}) == []


def test_focus_state_rejects_unknown_state_and_extra_keys():
    ok = {"state": "focus", "since": "2026-11-05T10:00:00+09:00", "as_of": "2026-11-05T12:00:00+09:00"}
    assert schemas.errors("focus-state", ok) == []
    assert schemas.errors("focus-state", {**ok, "state": "unknown"})
    assert schemas.errors("focus-state", {**ok, "window_title": "x"})


def test_daily_state_session_title_limit_and_tz():
    seg = {"start": "2026-11-05T09:00:00+09:00", "end": "2026-11-05T09:05:00+09:00", "state": "focus", "category": "work", "session_title": None}
    d = {"date": "2026-11-05", "tz": "Asia/Tokyo", "as_of": "2026-11-05T09:05:00+09:00", "segments": [seg]}
    assert schemas.errors("daily-state", d) == []
    seg2 = {**seg, "session_title": "あ" * 41}
    assert schemas.errors("daily-state", {**d, "segments": [seg2]})
    assert schemas.errors("daily-state", {**d, "tz": "UTC"})
    assert schemas.errors("daily-state", {**d, "segments": [{**seg, "window_title": "x"}]})  # 画面の文字の項目は無い（S-9.5）


def test_hibi_state_artifact_shape():
    s = {"now": {"state": "play", "since": "2026-11-05T10:00:00+09:00", "as_of": "2026-11-05T12:00:00+09:00"},
         "day": {"date": "2026-11-05", "segments": []}, "fetched_at": "2026-11-05T12:00:01+09:00"}
    assert schemas.errors("hibi-state", s) == []
    assert schemas.errors("hibi-state", {"now": s["now"], "day": s["day"]})  # fetched_at が無い


def test_job_request_tools_only_for_public_research_roles():
    base = {"role": "research", "prompt": "p", "sensitivity": "public", "tools": ["web-screenshot"]}
    assert schemas.errors("job-request", base) == []
    assert schemas.errors("job-request", {**base, "role": "impl"})
    assert schemas.errors("job-request", {**base, "sensitivity": "private"})
    assert schemas.errors("job-request", {**base, "tools": ["rm-rf"]})


def test_job_request_workdir_id_pattern():
    assert schemas.errors("job-request", {"role": "impl", "prompt": "p", "sensitivity": "private", "workdir_id": "Bad_ID"})
    assert schemas.errors("job-request", {"role": "impl", "prompt": "p", "sensitivity": "private", "workdir_id": "job-01"}) == []


def test_environment_cloud_sandbox_free_guard_rejects_only_that_environment():
    good = {"id": "box", "adapter": "cloud-sandbox", "capabilities": ["linux-sandbox"], "data_classes": ["public"], "registered_at": "2026-11-03",
            "cost": {"kind": "free"}, "max_concurrent": 1, "free_guard": {"machines": 1, "cost_max_usd": 0}}
    bad_cost = {**good, "id": "bad1", "cost": {"kind": "fixed"}}
    bad_conc = {**good, "id": "bad2", "max_concurrent": 2}
    bad_guard = {**good, "id": "bad3", "free_guard": {"machines": 2, "cost_max_usd": 0}}
    other = {"id": "archive-host", "adapter": "ssh-host", "capabilities": ["archive-store"], "data_classes": ["public", "private"], "registered_at": "2026-10-01"}
    ok, rejected = schemas.load_environments([good, bad_cost, bad_conc, bad_guard, other])
    assert [e["id"] for e in ok] == ["box", "archive-host"]
    assert sorted(r[0] for r in rejected) == ["bad1", "bad2", "bad3"]


def test_environment_capability_names_restricted():
    e = {"id": "x", "adapter": "ssh-host", "capabilities": ["deploy-target:hibi-server", "gpu"], "data_classes": ["public"], "registered_at": "2026-11-01"}
    assert schemas.errors("environment", e) == []
    assert schemas.errors("environment", {**e, "capabilities": ["Has Space"]})


def test_intake_destination_registry_example_ok():
    reg = yaml.safe_load("""
- name: control.command
  title: プラグインの制御
  schema: schemas/dest/control.command.json
  handler: http://control:8080/v1/commands
  allowed_from: [commander, hq]
  via: [local, ssh, tailnet, hq]
  tier: operate
  irreversible: false
  ack: sync
  default_expiry_min: 60
  conflict_key: "control:{target}:{op}:{args.name}"
  mcp_tool: ax_control
""")
    assert schemas.errors("intake-destinations", reg) == []
    reg[0]["tier"] = "root"
    assert schemas.errors("intake-destinations", reg)


def test_instruction_from_must_be_commander_or_hq():
    ins = {"from": "commander", "destination": "control.command", "payload": {}}
    assert schemas.errors("instruction", ins) == []
    assert schemas.errors("instruction", {**ins, "from": "manager"})
    assert schemas.errors("instruction", {**ins, "extra": 1})


def test_projection_response_names_fixed():
    r = {"name": "plugins", "upto_seq": 5, "rows": [{"key": "p", "value": {"mode": "run"}, "upto_seq": 5}]}
    assert schemas.errors("projection-response", r) == []
    assert schemas.errors("projection-response", {**r, "name": "other"})


def test_fragment_validation_for_defs():
    assert schemas.errors("validate", {"kind": "graph", "body": {}}, "/$defs/request") == []
    assert schemas.errors("validate", {"kind": "yaml", "body": {}}, "/$defs/request")
    assert schemas.errors("permission-check", {"allowed": True, "reason": "ok", "permission_id": None}, "/$defs/response") == []


def test_validate_raises_with_readable_errors():
    with pytest.raises(schemas.ValidationFailed) as ei:
        schemas.validate("healthz", {"ok": False})
    assert ei.value.errors


def test_cli_check_ok():
    assert schemas.main(["--check"]) == 0


def test_cli_validate_file(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"ok": True, "component": "c", "version": "1"}))
    assert schemas.main(["--validate", "healthz", str(f)]) == 0
    f.write_text(json.dumps({"ok": True}))
    assert schemas.main(["--validate", "healthz", str(f)]) == 1


def test_config_files_match_their_schemas(repo_root):
    for fname, schema in (("defaults.yaml", "defaults"), ("error-rules.yaml", "error-rules")):
        doc = yaml.safe_load((repo_root / "config" / fname).read_text(encoding="utf-8"))
        assert schemas.errors(schema, doc) == [], fname
    prof = yaml.safe_load((repo_root / "profile" / "example.yaml").read_text(encoding="utf-8"))
    assert schemas.errors("profile", prof) == []


def test_registry_yaml_matches_schema(repo_root):
    doc = yaml.safe_load((repo_root / "plugins" / "registry.yaml").read_text(encoding="utf-8"))
    assert schemas.errors("registry", doc) == []

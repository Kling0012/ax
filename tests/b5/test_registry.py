"""行き先の登録簿（S-6.3、D-5）の試験。T-6 の「登録簿に1行足すだけで行き先が増える」を含む。"""
from __future__ import annotations

import os

import pytest

from core.intake.registry import (Registry, build_registry, conflict_key_valid, discover_files,
                                  parse_files, render_conflict_key)

from .helpers import client_for, events_of, make_app, simple_dest, write_registry


def test_base_and_drop_dir_are_merged_and_later_wins(tmp_path):
    write_registry(tmp_path / "config", [simple_dest("a.one")], drop=[[simple_dest("b.two")]])
    r = Registry.load(tmp_path / "config")
    assert r.names() == ["a.one", "b.two"]


def test_entries_without_tier_or_via_fail_the_file(tmp_path):
    """tier と via は必須（S-6.9）。b0 の Schema が項目を必須にするため、ファイル単位で落ちる。"""
    entries = [simple_dest("ok.first"), simple_dest("no.via", via=None)]
    write_registry(tmp_path / "config", entries)
    r = Registry.load(tmp_path / "config")
    assert r.names() == []
    assert "registry_schema_failed" in r.faults


def test_irreversible_tier_mismatch_is_dropped(tmp_path):
    entries = [simple_dest("bad.mix", tier="operate", irreversible=True)]
    write_registry(tmp_path / "config", entries)
    r = Registry.load(tmp_path / "config")
    assert r.names() == []
    assert "irreversible_tier_mismatch" in r.faults


def test_broken_yaml_keeps_previous_registry_on_reload(tmp_path):
    write_registry(tmp_path / "config", [simple_dest()])
    r = Registry.load(tmp_path / "config")
    assert r.names() == ["demo.echo"]
    (tmp_path / "config" / "intake-destinations.yaml").write_text("- name: broken\n  title: [\n", encoding="utf-8")
    alerts: list[str] = []
    changed = r.maybe_reload(emit_alert=lambda reason, data: alerts.append(reason))
    assert changed is False
    assert r.names() == ["demo.echo"]  # 前の登録簿を使い続ける（D-5）
    assert alerts == ["registry_broken"]


def test_schema_violating_registry_keeps_previous(tmp_path):
    write_registry(tmp_path / "config", [simple_dest()])
    r = Registry.load(tmp_path / "config")
    import yaml

    (tmp_path / "config" / "intake-destinations.yaml").write_text(
        yaml.safe_dump([{"name": "x", "title": " tier も via も無い"}]), encoding="utf-8")
    changed = r.maybe_reload(emit_alert=lambda *_: None)
    assert changed is False and r.names() == ["demo.echo"]


def test_destination_removal_is_honored(tmp_path):
    write_registry(tmp_path / "config", [simple_dest()])
    r = Registry.load(tmp_path / "config")
    import yaml

    (tmp_path / "config" / "intake-destinations.yaml").write_text(yaml.safe_dump([]), encoding="utf-8")
    assert r.maybe_reload(emit_alert=lambda *_: None) is True
    assert r.names() == []  # 削除は利用者の変更なので従う


def test_missing_payload_schema_keeps_entry_and_reports_fault(tmp_path):
    import yaml

    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "intake-destinations.yaml").write_text(yaml.safe_dump([
        simple_dest("waiting.schema", schema="schemas/dest/waiting.schema.json")]), encoding="utf-8")
    r = Registry.load(cfg)
    # schemas/dest/ の本体は B12b2 が作る。それまでは登録だけして検査は緩く続ける
    assert r.names() == ["waiting.schema"]
    assert "payload_schema_missing" in r.faults
    assert r.get("waiting.schema")["_schema_missing"] is True


def test_broken_payload_schema_drops_entry(tmp_path, repo_root, monkeypatch):
    import shutil

    schemas_dir = tmp_path / "schemas"
    shutil.copytree(repo_root / "schemas", schemas_dir)
    (schemas_dir / "dest").mkdir(exist_ok=True)
    (schemas_dir / "dest" / "broken.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("AX_SCHEMAS_DIR", str(schemas_dir))
    from axcommon import schemas as ax_schemas

    ax_schemas.clear_cache()
    import yaml

    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "intake-destinations.yaml").write_text(yaml.safe_dump([
        simple_dest("broken.dest", schema="schemas/dest/broken.json")]), encoding="utf-8")
    r = Registry.load(cfg)
    assert r.names() == []
    assert "schema_load_failed" in r.faults
    ax_schemas.clear_cache()


def test_real_config_has_control_command_and_ledger_answer(repo_root):
    r = Registry.load(repo_root / "config")
    assert "control.command" in r.names() and "ledger.answer" in r.names()
    for name in ("control.command", "ledger.answer"):
        d = r.get(name)
        assert d["tier"] == "operate" and d["allowed_from"] == ["commander", "hq"]
        assert set(d["via"]) == {"local", "ssh", "tailnet", "hq"}
    # B12b2 以後: destination schema が実在し、正準の schemas/dest/ を指す。
    assert not any(r.get(n)["_schema_missing"] for n in ("control.command", "ledger.answer"))
    assert r.get("control.command")["_schema_name"] == "dest/control.command.json"
    assert r.get("ledger.answer")["_schema_name"] == "dest/ledger.answer.json"
    assert r.get("control.command")["mcp_tool"] == "ax_control"


def test_one_more_line_adds_a_destination_without_restart(fake_transport, tmp_path):
    """T-6: 登録簿に1行足すだけで行き先が増える。"""
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    fake_transport.add("POST", "/v1/echo", json={"echoed": True})
    entries = [simple_dest("demo.echo")]
    app = make_app(tmp_path, fake_transport, entries)
    import yaml

    write_registry(tmp_path / "config", entries + [simple_dest("demo.second", handler="http://demo:8080/v1/second")])
    fake_transport.add("POST", "/v1/second", json={"second": True})
    with client_for(app) as c:
        names = [d["name"] for d in c.get("/v1/destinations").json()["destinations"]]
        assert names == ["demo.echo", "demo.second"]  # 再起動なし
        r = c.post("/v1/instructions", json={"from": "commander", "destination": "demo.second",
                                             "payload": {"ok": True, "component": "d", "version": "1"}})
        assert r.status_code == 200 and r.json()["result"] == {"second": True}
        assert [p.path for p in fake_transport.calls if p.path == "/v1/second"] == ["/v1/second"]


def test_conflict_key_format():
    assert conflict_key_valid("control:{target}:{op}:{args.name}")
    assert conflict_key_valid("card:{todo_id}")
    assert conflict_key_valid(None) and conflict_key_valid("")
    assert not conflict_key_valid("control:{target|op}")   # 式は書けない
    assert not conflict_key_valid("card:{todo_id}}")
    assert not conflict_key_valid("x:{a.b.c} y")           # 式の混入


def test_conflict_key_render_walks_nested_payload():
    assert render_conflict_key("control:{target}:{op}:{args.name}",
                               {"target": "issue-automation", "op": "set_knob", "args": {"name": "k"}}) == "control:issue-automation:set_knob:k"
    assert render_conflict_key("card:{todo_id}", {}) == "card:"  # 無い項目は空


def test_faults_at_startup_emit_health_alert(fake_transport, tmp_path):
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    app = make_app(tmp_path, fake_transport, [simple_dest("bad.mix", tier="operate", irreversible=True)])
    with client_for(app):
        pass
    alerts = [e for e in events_of(fake_transport, "health.alert")
              if (e.get("data") or {}).get("check") == "intake_registry_faults"]
    assert alerts
    assert "irreversible_tier_mismatch" in alerts[0]["data"]["target"]

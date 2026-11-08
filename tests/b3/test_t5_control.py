"""T-5 制御 API: 検証の拒否、期限、優先順位（S-5.4、S-5.5、S-5.6、D-4）。

中核の口（8080 相当）を TestClient で試す。発行者のトークンは conftest の秘密置き場。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from axcommon import timeutil

from tests.b3.conftest import (TOK_BACKUP, TOK_EMERGENCY, TOK_HQ, TOK_INSPECTION, TOK_INTAKE, TOK_MANAGER)

MGR = {"Authorization": "Bearer " + TOK_MANAGER}
INS = {"Authorization": "Bearer " + TOK_INSPECTION}
EMG = {"Authorization": "Bearer " + TOK_EMERGENCY}
BK = {"Authorization": "Bearer " + TOK_BACKUP}
USER = {"Authorization": "Bearer " + TOK_INTAKE}  # 入口が hq で受けた利用者の指示を中継する資格


def _in(minutes):
    return timeutil.format_ts(timeutil.now() + timedelta(minutes=minutes))


def _issue(env, headers, body, expect=None):
    if headers == INS and "on_expiry" not in body:
        body = {**body, "on_expiry": "release"}
    r = env["core"].post("/v1/commands", json=body, headers=headers)
    if expect is not None:
        assert r.status_code == expect, f"{r.status_code} {r.text} for {body}"
    return r


def _set_mode(target, mode, reason="点検", **extra):
    return {"target": target, "op": "set_mode", "args": {"mode": mode}, "reason": reason, **extra}


def _set_knob(target, name, value, reason="調整", **extra):
    return {"target": target, "op": "set_knob", "args": {"name": name, "value": value}, "reason": reason, **extra}


def _effective(env, plugin="issue-automation"):
    return env["core"].get(f"/v1/plugins/{plugin}/effective").json()


def _kinds(env):
    return [c.json["kind"] for c in env["transport"].calls if c.method == "POST" and c.json and c.json.get("kind")]


# ---- 登録（S-4.1、D-4.1）----


def test_registration_reads_registry_yaml(control_env):
    env = control_env
    assert env["register"]["registered"] == ["issue-automation", "worker-pool"]
    plugins = env["core"].get("/v1/plugins").json()["plugins"]
    assert {p["id"] for p in plugins} == {"issue-automation", "worker-pool"}
    view = env["core"].get("/v1/plugins/issue-automation").json()
    assert view["manifest"]["id"] == "issue-automation" and view["manifest"]["knobs"]["every_s"]["max"] == 3600
    eff = _effective(env)
    assert eff["mode"] == "run" and eff["knobs"]["every_s"] == 300 and eff["revision"] >= 1


# ---- 検証の拒否（S-5.4、T-5）----


def test_t5_undeclared_knob_and_out_of_range_are_rejected(control_env):
    env = control_env
    r = _issue(env, MGR, _set_knob("issue-automation", "no_such_knob", 1, expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "knob_not_declared"
    r = _issue(env, MGR, _set_knob("issue-automation", "every_s", 10, expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "knob_out_of_range"
    r = _issue(env, MGR, _set_knob("issue-automation", "every_s", 999999, expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "knob_out_of_range"
    r = _issue(env, MGR, _set_knob("issue-automation", "model", "premium", expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "knob_out_of_range"
    # 宣言済みで範囲内は受理
    r = _issue(env, MGR, _set_knob("issue-automation", "every_s", 120, expires_at=_in(30)))
    assert r.status_code == 201, r.text
    assert _effective(env)["knobs"]["every_s"] == 120


def test_t5_manager_command_without_expiry_and_core_target_and_fleet_are_rejected(control_env):
    env = control_env
    # 期限のない管理役の命令
    r = _issue(env, MGR, _set_mode("issue-automation", "collect_only"))
    assert r.status_code == 400 and r.json()["error"]["code"] == "lease_required"
    # 中核（台帳）への命令
    r = _issue(env, MGR, _set_mode("ledger", "collect_only", expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_target"
    # 管理役の全体命令
    r = _issue(env, MGR, _set_mode("all", "collect_only", expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "multi_target_forbidden"
    # 本文の issued_by が資格と違う
    r = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(30), issued_by="user"))
    assert r.status_code == 400 and r.json()["error"]["code"] == "issuer_mismatch"
    # reason が空
    r = _issue(env, MGR, _set_mode("issue-automation", "collect_only", reason="", expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "reason_required"
    # capabilities に無い mode
    r = _issue(env, MGR, _set_mode("issue-automation", "nosuch", expires_at=_in(30)))
    assert r.status_code in (400, 422)


def test_t5_non_user_commands_beyond_control_max_lease_min_are_rejected(control_env):
    env = control_env
    from axcommon.defaults import Defaults

    lease = float(Defaults.load().get("control_max_lease_min"))
    r = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(lease + 10)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "lease_too_long"
    r = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(lease - 5)))
    assert r.status_code == 201, r.text


def test_t5_inspection_commands_are_accepted_up_to_drain_max_h(control_env):
    env = control_env
    from axcommon.defaults import Defaults

    drain_h = float(Defaults.load().get("drain_max_h"))
    r = _issue(env, INS, _set_mode("issue-automation", "collect_only", expires_at=_in(drain_h * 60 - 30), on_expiry="release"))
    assert r.status_code == 201, r.text
    # 点検の命令は drain_max_h 以内。control_max_lease_min を超えても受理する
    assert drain_h * 60 - 30 > float(Defaults.load().get("control_max_lease_min"))
    r = _issue(env, INS, _set_mode("issue-automation", "poll_paused", expires_at=_in(drain_h * 60 + 30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "lease_too_long"


def test_t5_expired_pending_keeps_effect_and_release_on_expiry(control_env):
    env = control_env
    r = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(30)))
    assert r.status_code == 201
    cmd_id = r.json()["id"]
    assert _effective(env)["mode"] == "collect_only"
    cmds = env["core"].get("/v1/commands", params={"state": "active"}).json()["commands"]
    assert any(c["id"] == cmd_id for c in cmds)  # まだ期限前
    env["clock"].advance(31 * 60)
    changed = env["service"].sweep_expiry()
    assert any(c["id"] == cmd_id and c["state"] == "expired_pending" for c in changed)
    # 効力は残る（S-5.6）
    assert _effective(env)["mode"] == "collect_only"
    kinds = _kinds(env)
    assert "control.expired_pending" in kinds
    # 出した者の解除
    r = env["core"].post(f"/v1/commands/{cmd_id}/release", json={"reason": "戻す"}, headers=MGR)
    assert r.status_code == 200 and r.json()["state"] == "released"
    assert _effective(env)["mode"] == "run"


def test_t5_inspection_command_is_released_at_expiry(control_env):
    env = control_env
    r = _issue(env, INS, _set_mode("issue-automation", "draining", expires_at=_in(10), on_expiry="release"))
    assert r.status_code == 201, r.text
    cmd_id = r.json()["id"]
    assert _effective(env)["mode"] == "draining"
    env["clock"].advance(11 * 60)
    changed = env["service"].sweep_expiry()
    assert any(c["id"] == cmd_id and c["state"] == "released" for c in changed)
    assert _effective(env)["mode"] == "run"
    kinds = _kinds(env)
    assert "control.released" in kinds


# ---- 優先順位（S-5.5）----


def test_t5_effective_value_follows_priority_and_recency(control_env):
    env = control_env
    r0 = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(50)), expect=201)
    mgr0_id = r0.json()["id"]
    assert _effective(env)["mode"] == "collect_only"
    # 点検は管理役より強い
    r = _issue(env, INS, _set_mode("issue-automation", "poll_paused", expires_at=_in(50)), expect=201)
    ins_id = r.json()["id"]
    assert _effective(env)["mode"] == "poll_paused"
    # 利用者は点検より強い（入口の中継。期限なしでよい）
    permission = env["core"].post("/v1/permissions", json={
        "project": "ax", "repo": "ax", "destination": "local", "operations": ["plugin.run:issue-automation"],
        "source_instruction": "priority-test", "issued_by": "user"}, headers=USER)
    assert permission.status_code == 201
    run = _set_mode("issue-automation", "run", issued_by="user", reason="利用者の指示")
    run["args"]["permission_ids"] = [permission.json()["id"]]
    r = _issue(env, USER, run, expect=201)
    user_id = r.json()["id"]
    assert _effective(env)["mode"] == "run"
    # 緊急は利用者より強い
    r = _issue(env, EMG, _set_mode("issue-automation", "draining", expires_at=_in(20)), expect=201)
    emg_id = r.json()["id"]
    assert _effective(env)["mode"] == "draining"
    # 生きている命令がなくなれば manifest の既定値に戻る
    for cid, hdr, body in ((emg_id, EMG, {"reason": "戻す"}),
                           (user_id, USER, {"reason": "戻す", "issued_by": "user"}),
                           (ins_id, INS, {"reason": "戻す"}),
                           (mgr0_id, MGR, {"reason": "戻す"})):
        r = env["core"].post(f"/v1/commands/{cid}/release", json=body, headers=hdr)
        assert r.status_code == 200, f"{cid}: {r.text}"
    assert _effective(env)["mode"] == "run"
    # 同じ優先順位では新しい命令が勝つ（管理役2件）
    _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(50)), expect=201)
    _issue(env, MGR, _set_mode("issue-automation", "poll_paused", expires_at=_in(50)), expect=201)
    assert _effective(env)["mode"] == "poll_paused"
    # knob も同じ規則
    _issue(env, MGR, _set_knob("issue-automation", "every_s", 120, expires_at=_in(50)), expect=201)
    _issue(env, MGR, _set_knob("issue-automation", "every_s", 600, expires_at=_in(50)), expect=201)
    assert _effective(env)["knobs"]["every_s"] == 600


def test_user_only_knob_who_is_enforced_and_user_marked_value_is_protected(control_env):
    """S-4.3 の who と S-5.4 の 7（利用者設定の印）。"""
    env = control_env
    # who: [user] の knob は管理役は変えられない
    r = _issue(env, MGR, _set_knob("worker-pool", "free_min_success", 0.5, expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "who_not_allowed"
    # 利用者は変えられる（who が無い knob で印の試験をする）
    r = _issue(env, USER, _set_knob("worker-pool", "family_cap", 5, issued_by="user", reason="利用者の指示"))
    assert r.status_code == 201, r.text
    assert _effective(env, "worker-pool")["knobs"]["family_cap"] == 5
    # 印が付いた値は管理役は変えられない
    r = _issue(env, MGR, _set_knob("worker-pool", "family_cap", 6, expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "user_set_knob"


def test_renew_has_limit(control_env):
    """S-5.6: 延長は1つの命令につき control_max_renewals 回まで。"""
    env = control_env
    from axcommon.defaults import Defaults

    limit = int(Defaults.load().get("control_max_renewals"))
    r = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(30)))
    cmd_id = r.json()["id"]
    for i in range(limit):
        rr = env["core"].post(f"/v1/commands/{cmd_id}/renew", json={"expires_at": _in(30), "reason": f"{i}回目"},
                              headers=MGR)
        assert rr.status_code == 200, rr.text
    rr = env["core"].post(f"/v1/commands/{cmd_id}/renew", json={"expires_at": _in(30), "reason": "超える"},
                          headers=MGR)
    assert rr.status_code == 409 and rr.json()["error"]["code"] == "renewals_exhausted"


# ---- POST /v1/validate（S-5.7、F-14.3）----


def test_validate_manifest(control_env):
    env = control_env
    import yaml

    from tests.b3.conftest import CONTROL_MANIFESTS

    good = yaml.safe_load(CONTROL_MANIFESTS["plugins/subloop/issue-automation/manifest.yaml"])
    r = env["core"].post("/v1/validate", json={"kind": "manifest", "body": good})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["errors"] == []
    bad = dict(good)
    bad["capabilities"] = ["run"]  # draining が必須
    r = env["core"].post("/v1/validate", json={"kind": "manifest", "body": bad})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["errors"]
    # 何も保存しない（登録簿は変わらない）
    assert len(env["core"].get("/v1/plugins").json()["plugins"]) == 2


def test_validate_graph_rejects_invalid_body(control_env):
    """kind=graph は B7 の検査器で検証する。"""
    env = control_env
    r = env["core"].post("/v1/validate", json={"kind": "graph", "body": {"nodes": {}}})
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["errors"]


# ---- 作業側の口（F-18.4 の表）----


def test_work_mouth_is_read_only(control_env):
    env = control_env
    from tests.b3.conftest import TOK_DIGEST

    h = {"Authorization": "Bearer " + TOK_DIGEST}
    assert env["work"].get("/v1/plugins", headers=h).status_code == 200
    eff = env["work"].get("/v1/plugins/issue-automation/effective", headers=h)
    assert eff.status_code == 200 and eff.json()["mode"] == "run"
    assert env["work"].get("/v1/plugins/issue-automation", headers=h).status_code == 200
    # 資格が無ければ 401
    assert env["work"].get("/v1/plugins/issue-automation/effective").status_code == 401
    # 書き込み系と項目の中継は通さない
    assert env["work"].post("/v1/commands", json={"target": "issue-automation", "op": "run_now", "args": {}, "reason": "x"},
                            headers=h).status_code == 404
    assert env["work"].post("/v1/plugins/issue-automation/items", json={}, headers=h).status_code == 404
    assert env["work"].post("/v1/validate", json={"kind": "manifest", "body": {}}, headers=h).status_code == 404


# ---- 項目の中継（S-5.7）と /apply（S-4.4、D-4.2）----


def test_items_relay_and_apply_use_the_fakes(control_env):
    env = control_env
    from tests.b3.conftest import TOK_APPLY

    t = env["transport"]
    t.add("POST", "/items", status=202, json={"accepted": True, "item_key": "k1"})
    t.add("POST", "/apply", status=200, json={"ok": True})
    t.add("GET", "/status", status=200, json={"mode": "collect_only", "knobs": {}, "running": [], "queued": 0, "last_run": None})
    r = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(30)))
    assert r.status_code == 201
    # 対象の /apply を呼ぶ（control の資格だけが通る口。ここでは偽の転送で確認）
    applies = [c for c in t.calls if c.path == "/apply"]
    assert applies and applies[-1].headers.get("Authorization") == "Bearer " + TOK_APPLY
    assert applies[-1].json["mode"] == "collect_only" and "revision" in applies[-1].json
    # 項目の中継は入口（intake）の資格だけ
    r = env["core"].post("/v1/plugins/issue-automation/items", json={"item_key": "k1"}, headers=MGR)
    assert r.status_code == 403
    r = env["core"].post("/v1/plugins/issue-automation/items", json={"item_key": "k1"}, headers=USER)
    assert r.status_code == 202, r.text
    # accepts_items で無ければ 409
    t.add("POST", "/items", status=409, json={"error": {"code": "inbox_full"}})
    r = env["core"].post("/v1/plugins/worker-pool/items", json={"item_key": "k1"}, headers=USER)
    assert r.status_code == 409


def test_relay_returns_503_when_subloop_is_down(control_env):
    env = control_env
    env["transport"].down()
    r = env["core"].post("/v1/plugins/issue-automation/items", json={"item_key": "k2"}, headers=USER)
    assert r.status_code == 503


# ---- タイマー（S-7.8、D-4.4）と継続許可（S-20.3）----


def test_timer_creation_rules(control_env):
    env = control_env
    cid = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(30)), expect=201).json()["id"]
    body = {"at": _in(60), "action": "control.release", "command_id": cid, "reason": "点検の戻し"}
    # 管理役以外は作れない
    r = env["core"].post("/v1/timers", json=body, headers=INS)
    assert r.status_code == 403
    r = env["core"].post("/v1/timers", json=body, headers=MGR)
    assert r.status_code == 201, r.text
    # action が違う
    r = env["core"].post("/v1/timers", json={**body, "action": "reboot"}, headers=MGR)
    assert r.status_code == 422
    # plugin.run_now には対象が要る
    r = env["core"].post("/v1/timers", json={"at": _in(60), "action": "plugin.run_now", "reason": "1回だけ"}, headers=MGR)
    assert r.status_code == 422
    # 寿命は timer_max_days まで
    from axcommon.defaults import Defaults

    days = float(Defaults.load().get("timer_max_days"))
    r = env["core"].post("/v1/timers", json={**body, "expires_at": timeutil.format_ts(
        timeutil.now() + timedelta(days=days + 1))}, headers=MGR)
    assert r.status_code == 422 and r.json()["error"]["code"] == "timer_max_days"
    # 一覧は timer_max_count まで
    count = int(Defaults.load().get("timer_max_count"))
    made = 1
    while made < count:
        rr = env["core"].post("/v1/timers", json={**body, "at": _in(60 + made)}, headers=MGR)
        assert rr.status_code == 201, rr.text
        made += 1
    r = env["core"].post("/v1/timers", json={**body, "at": _in(9999)}, headers=MGR)
    assert r.status_code == 409 and r.json()["error"]["code"] == "timer_max_count"


def test_permission_check_and_lifecycle(control_env):
    env = control_env
    r = env["core"].post("/v1/permissions/check", json={"project": "dreamforge", "repo": "dreamforge-rpg",
                                                        "destination": "github", "operation": "merge"})
    assert r.status_code == 200 and r.json()["allowed"] is False
    body = {"project": "dreamforge", "repo": "dreamforge-rpg", "destination": "github",
            "operations": ["merge", "release"], "source_instruction": "instr-1", "issued_by": "user"}
    # 管理役は許可を作れない（S-20.3）
    r = env["core"].post("/v1/permissions", json=body, headers=MGR)
    assert r.status_code == 403
    r = env["core"].post("/v1/permissions", json=body, headers=USER)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = env["core"].post("/v1/permissions/check", json={"project": "dreamforge", "repo": "dreamforge-rpg",
                                                        "destination": "github", "operation": "merge"})
    assert r.json()["allowed"] is True and r.json()["permission_id"] == pid
    # 範囲外
    r = env["core"].post("/v1/permissions/check", json={"project": "dreamforge", "repo": "other-repo",
                                                        "destination": "github", "operation": "merge"})
    assert r.json()["allowed"] is False
    # 無効化
    r = env["core"].post(f"/v1/permissions/{pid}/disable", json={"issued_by": "user"}, headers=USER)
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = env["core"].post("/v1/permissions/check", json={"project": "dreamforge", "repo": "dreamforge-rpg",
                                                        "destination": "github", "operation": "merge"})
    assert r.json()["allowed"] is False


# ---- 引継ぎ後の境界と期限の確認 ----


def test_inspection_must_release_and_commander_cannot_address_all(control_env):
    env = control_env
    r = _issue(env, INS, _set_mode("issue-automation", "draining", expires_at=_in(30), on_expiry="ask_issuer"))
    assert r.status_code == 400 and r.json()["error"]["code"] == "on_expiry_required"
    r = _issue(env, USER, _set_mode("all", "draining", issued_by="commander", expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "multi_target_forbidden"


def test_foreign_release_and_renew_of_released_command_are_rejected(control_env):
    env = control_env
    cmd = _issue(env, INS, _set_mode("issue-automation", "draining", expires_at=_in(30)), expect=201).json()
    url = f"/v1/commands/{cmd['id']}"
    assert env["core"].post(url + "/release", json={}, headers=MGR).status_code == 403
    assert _effective(env)["mode"] == "draining"
    assert env["core"].post(url + "/release", json={}, headers=INS).status_code == 200
    assert env["core"].post(url + "/renew", json={"expires_at": _in(20), "reason": "延長"}, headers=INS).status_code == 409
    assert _effective(env)["mode"] == "run"


def test_renew_validates_lease_and_timestamp(control_env):
    env = control_env
    cmd = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(30)), expect=201).json()
    url = f"/v1/commands/{cmd['id']}/renew"
    r = env["core"].post(url, json={"expires_at": _in(61), "reason": "延長"}, headers=MGR)
    assert r.status_code == 400 and r.json()["error"]["code"] == "lease_too_long"
    for bad in ("not-a-time", 5, [], "2026-99-99T99:00:00.000Z"):
        r = env["core"].post(url, json={"expires_at": bad, "reason": "延長"}, headers=MGR)
        assert r.status_code == 422
    env["clock"].advance(31 * 60)
    env["service"].sweep_expiry()
    assert env["core"].post(url, json={"expires_at": _in(20), "reason": "継続"}, headers=MGR).status_code == 200
    assert env["service"].store.get_command(cmd["id"])["state"] == "active"


def test_registration_preserves_revision_user_marks_and_checks_graph(control_env, monkeypatch):
    env = control_env
    _issue(env, USER, _set_knob("issue-automation", "every_s", 120, issued_by="user"), expect=201)
    revision = _effective(env)["revision"]
    env["service"].register()
    assert _effective(env)["revision"] > revision
    r = _issue(env, MGR, _set_knob("issue-automation", "every_s", 180, expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "user_set_knob"
    graph = env["root"] / "plugins/subloop/issue-automation/graph.yaml"
    graph.write_text("nodes: {}\n", encoding="utf-8")
    result = env["service"].register()
    assert result["registered"] == ["worker-pool"]
    assert result["rejected"][0][0] == "issue-automation"
    assert env["core"].get("/v1/plugins/issue-automation/effective").status_code == 404
    assert env["core"].get("/v1/plugins").status_code == 200
    monkeypatch.setenv("AX_GRAPH_CHECK", str(env["tmp_path"] / "missing.py"))
    result = env["service"].register()
    assert "issue-automation" not in result["registered"]


def test_validate_checks_defaults_and_real_graph_without_saving(control_env):
    import copy
    import yaml
    from tests.b3.conftest import CONTROL_MANIFESTS
    env = control_env
    before = env["service"].store.list_plugins()
    graph = yaml.safe_load(CONTROL_MANIFESTS["plugins/subloop/issue-automation/graph.yaml"])
    r = env["core"].post("/v1/validate", json={"kind": "graph", "body": graph})
    assert r.status_code == 200 and r.json() == {"ok": True, "errors": []}
    manifest = yaml.safe_load(CONTROL_MANIFESTS["plugins/subloop/issue-automation/manifest.yaml"])
    for bad in (10, True, "300"):
        doc = copy.deepcopy(manifest)
        doc["knobs"]["every_s"]["default"] = bad
        r = env["core"].post("/v1/validate", json={"kind": "manifest", "body": doc})
        assert r.status_code == 200 and r.json()["ok"] is False
    assert env["service"].store.list_plugins() == before


def test_global_user_knob_marks_every_target(control_env):
    env = control_env
    # 両方に存在する knob を用意する。
    p = env["service"].store.get_plugin("worker-pool")
    p["manifest"]["knobs"]["every_s"] = env["service"].store.get_plugin("issue-automation")["manifest"]["knobs"]["every_s"]
    env["service"].store.upsert_plugin(p)
    cmd = _issue(env, USER, _set_knob("all", "every_s", 120, issued_by="user"), expect=201).json()
    for pid in ("issue-automation", "worker-pool"):
        r = _issue(env, MGR, _set_knob(pid, "every_s", 180, expires_at=_in(30)))
        assert r.status_code == 400 and r.json()["error"]["code"] == "user_set_knob"
    r = env["core"].post(f"/v1/commands/{cmd['id']}/release", json={"issued_by": "user"}, headers=USER)
    assert r.status_code == 200
    assert _issue(env, MGR, _set_knob("issue-automation", "every_s", 180, expires_at=_in(30))).status_code == 201


def test_release_pushes_default_and_applied_requires_knobs(control_env):
    env = control_env
    t = env["transport"]
    t.add("POST", "/apply", json={"ok": True})
    t.add("GET", "/status", json={"mode": "collect_only", "knobs": {}})
    cmd = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(30)), expect=201).json()
    assert "control.applied" not in _kinds(env)
    knobs = _effective(env)["knobs"]
    t.add("GET", "/status", json={"mode": "collect_only", "knobs": knobs})
    env["service"].apply_to(cmd)
    assert "control.applied" in _kinds(env)
    env["core"].post(f"/v1/commands/{cmd['id']}/release", json={}, headers=MGR)
    assert [c for c in t.calls if c.path == "/apply"][-1].json["mode"] == "run"


def test_timer_owns_command_and_releases_at_due_time(control_env):
    env = control_env
    owned = _issue(env, MGR, _set_mode("issue-automation", "collect_only", expires_at=_in(30)), expect=201).json()
    foreign = _issue(env, INS, _set_mode("issue-automation", "draining", expires_at=_in(30)), expect=201).json()
    body = {"at": _in(5), "action": "control.release", "command_id": foreign["id"], "reason": "解除"}
    assert env["core"].post("/v1/timers", json=body, headers=MGR).status_code == 403
    body["command_id"] = owned["id"]
    r = env["core"].post("/v1/timers", json=body, headers=MGR)
    assert r.status_code == 201
    tid = r.json()["id"]
    env["clock"].advance(5 * 60)
    env["service"].sweep_timers()
    assert env["service"].store.get_command(owned["id"])["state"] == "released"
    assert env["service"].store.get_command(foreign["id"])["state"] == "active"
    t = env["service"].store.get_timer(tid)
    assert t["state"] == "fired" and t["fired_count"] == 1
    assert env["service"].fire_timer(tid) == t
    assert env["service"].sweep_timers() == []


def test_timer_interval_expiry_duplicate_id_and_malformed_input(control_env):
    env = control_env
    body = {"every_s": 60, "action": "ledger.recheck", "reason": "確認", "expires_at": _in(4)}
    r = env["core"].post("/v1/timers", json=body, headers=MGR)
    assert r.status_code == 201
    tid = r.json()["id"]
    assert r.json()["at"] == _in(1)
    assert env["core"].post("/v1/timers", json={**body, "id": tid}, headers=MGR).status_code == 409
    for extra in ({"at": _in(1)}, {"every_s": True}, {"every_s": -1}, {"every_s": 0}, {"expires_at": "bad"}):
        assert env["core"].post("/v1/timers", json={**body, **extra}, headers=MGR).status_code == 422
    env["clock"].advance(60)
    assert env["service"].fire_timer(tid)["fired_count"] == 1
    env["clock"].advance(181)
    env["service"].sweep_timers()
    t = env["service"].store.get_timer(tid)
    assert t["state"] == "expired" and t["fired_count"] == 1
    assert env["service"].fire_timer(tid) == t
    assert env["core"].post(f"/v1/timers/{tid}/fire", json={}, headers=MGR).status_code == 501


def test_post_bodies_and_maintenance_require_valid_input_and_credential(control_env):
    env = control_env
    assert env["core"].post("/v1/sweep", json={}).status_code == 401
    for path in ("/v1/commands", "/v1/permissions", "/v1/timers", "/v1/validate"):
        r = env["core"].post(path, json=[1], headers=MGR)
        assert r.status_code == 422
    assert env["core"].post("/v1/sweep", json={}, headers=MGR).status_code == 200


def test_non_setting_operations_do_not_claim_to_apply(control_env):
    env = control_env
    r = _issue(env, MGR, {"target": "issue-automation", "op": "run_now", "args": {}, "reason": "実行", "expires_at": _in(30)})
    assert r.status_code == 501 and r.json()["error"]["code"] == "operation_not_implemented"
    assert env["service"].list_commands() == []


def test_core_and_work_binds_are_separate():
    from core.control.__main__ import resolve_binds

    assert resolve_binds({}, addresses=["198.51.100.23", "127.0.0.1", "192.0.2.23"]) == (["192.0.2.23", "127.0.0.1"], ["198.51.100.23"])
    assert resolve_binds({}, addresses=["127.0.0.1", "198.51.100.23"]) == (["127.0.0.1"], ["198.51.100.23"])
    assert resolve_binds({}, addresses=[]) == (["127.0.0.1"], [])
    assert resolve_binds(
        {"AX_CONTROL_CORE_BIND": "192.0.2.11", "AX_CONTROL_WORK_BIND": "192.0.2.12"}, addresses=[]
    ) == (["192.0.2.11", "127.0.0.1"], ["192.0.2.12"])


def test_exclusion_lease_uses_same_limit_and_no_operation_is_silently_lost(control_env):
    env = control_env
    until = _in(61)
    r = _issue(env, MGR, {"target": "worker-pool", "op": "exclude_resource", "args": {"resource_id": "model-a", "until": until},
                          "expires_at": until, "on_expiry": "release", "reason": "除外"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "lease_too_long"
    env["service"].exclusion_adapter = type("Adapter", (), {"sync": lambda self, command, active: None})()
    until = _in(20)
    r = _issue(env, MGR, {"target": "worker-pool", "op": "exclude_resource", "args": {"resource_id": "model-a", "until": until},
                          "expires_at": until, "on_expiry": "release", "reason": "除外"})
    assert r.status_code == 201
    commands = env["service"].list_commands()
    assert len(commands) == 1 and commands[0]["op"] == "exclude_resource"


def test_commander_priority_is_between_inspection_and_manager_and_backup(control_env):
    env = control_env
    _issue(env, BK, _set_mode("issue-automation", "collect_only", expires_at=_in(20)), expect=201)
    _issue(env, USER, _set_mode("issue-automation", "poll_paused", issued_by="commander", expires_at=_in(20)), expect=201)
    assert _effective(env)["mode"] == "poll_paused"
    _issue(env, INS, _set_mode("issue-automation", "draining", expires_at=_in(20)), expect=201)
    assert _effective(env)["mode"] == "draining"
    assert _issue(env, USER, _set_mode("issue-automation", "run", issued_by="commander")).status_code == 400


def test_timer_malformed_target_and_oversized_interval_are_rejected(control_env):
    env = control_env
    for body in ({"every_s": 10**30, "action": "ledger.recheck"},
                 {"at": _in(2), "action": "plugin.run_now", "target": []},
                 {"at": _in(2), "action": "plugin.run_now", "target": "all"},
                 {"at": _in(2), "action": "control.release", "command_id": {}}):
        r = env["core"].post("/v1/timers", json={**body, "reason": "確認"}, headers=MGR)
        assert r.status_code == 422


def test_sending_plugin_starts_collecting_and_never_starts_without_destination_contract(control_env):
    env = control_env
    p = env["service"].store.get_plugin("issue-automation")
    p["manifest"]["outputs"] = ["x.post"]
    env["service"].store.upsert_plugin(p)
    assert _effective(env)["mode"] == "collect_only"
    r = _issue(env, MGR, _set_mode("issue-automation", "run", expires_at=_in(30)))
    assert r.status_code == 400 and r.json()["error"]["code"] == "user_only"
    r = _issue(env, USER, _set_mode("issue-automation", "run", issued_by="user"))
    assert r.status_code == 400 and r.json()["error"]["code"] == "destination_contract_unavailable"
    assert _effective(env)["mode"] == "collect_only"


def test_permission_invalid_shapes_wildcards_and_unverifiable_conditions_are_denied(control_env):
    env = control_env
    body = {"project": "p", "repo": "repo", "destination": "github", "operations": ["merge"],
            "source_instruction": "instr-1", "issued_by": "user"}
    for extra in ({"repo": "*"}, {"destination": "*"}, {"operations": [{}]}, {"operations": ["merge", "merge"]},
                  {"operations": ["comment"]}, {"conditions": []}):
        r = env["core"].post("/v1/permissions", json={**body, **extra}, headers=USER)
        assert r.status_code == 422
    r = env["core"].post("/v1/permissions", json={**body, "conditions": {"safe": True}}, headers=USER)
    assert r.status_code == 201
    check = {"project": "p", "repo": "repo", "destination": "github", "operation": "merge"}
    assert env["core"].post("/v1/permissions/check", json=check).json()["allowed"] is False


def test_core_target_is_forbidden_even_if_in_registry(control_env):
    env = control_env
    p = env["service"].store.get_plugin("worker-pool")
    p["id"] = "ledger"
    p["manifest"]["id"] = "ledger"
    env["service"].store.upsert_plugin(p)
    assert _issue(env, MGR, _set_mode("ledger", "draining", expires_at=_in(30))).status_code == 400
    _issue(env, INS, _set_mode("all", "draining", expires_at=_in(30)), expect=201)
    assert env["core"].get("/v1/plugins/ledger/effective").status_code == 404


def test_t49_selection_controls_use_authenticated_issuer_and_show_user_marks(control_env):
    env = control_env
    request = _set_knob("worker-pool", "family_cap", 5, issued_by="user")
    assert _issue(env, MGR, {**request, "expires_at": _in(20)}).status_code == 400
    assert _issue(env, USER, request).status_code == 201
    assert _issue(env, USER, _set_knob("worker-pool", "family_cap", 6, issued_by="user")).status_code == 201
    view = env["core"].get("/v1/plugins/worker-pool").json()
    assert view["knobs"]["family_cap"] == 6
    assert view["user_set_knobs"]["family_cap"] is True
    assert _issue(env, MGR, _set_knob("worker-pool", "family_cap", 7, expires_at=_in(20))).status_code == 400
    assert _effective(env, "worker-pool")["knobs"]["family_cap"] == 6


def test_t49_exclusion_adapter_releases_only_the_target_command(control_env):
    env = control_env
    from core.control.selection_exclusions import ExclusionAdapter
    class Client:
        def __init__(self):
            self.active = {}
            self.stopped = {"model-a"}
        def exclude(self, *, command_id, resource_id, until, reason):
            self.active[command_id] = resource_id
        def release(self, *, command_id, resource_id):
            self.active.pop(command_id, None)
    adapter = Client()
    env["service"].exclusion_adapter = ExclusionAdapter(adapter)
    until = _in(20)
    body = {"target": "worker-pool", "op": "exclude_resource", "args": {"resource_id": "model-a", "until": until},
            "expires_at": until, "on_expiry": "release", "reason": "temporary"}
    first = _issue(env, MGR, body, expect=201).json()
    second = _issue(env, MGR, body, expect=201).json()
    assert set(adapter.active) == {first["id"], second["id"]}
    response = env["core"].post("/v1/commands/" + first["id"] + "/release", json={"reason": "done"}, headers=MGR)
    assert response.status_code == 200
    assert set(adapter.active) == {second["id"]}
    assert adapter.stopped == {"model-a"}


def test_t49_composite_settings_public_api_through_pool(control_env, monkeypatch):
    from axcommon.creds import sha256_hex
    table = control_env["tmp_path"] / "pool-core-tokens"
    table.write_text("intake " + sha256_hex("test-intake") + "\n")
    monkeypatch.setenv("AX_CORE_TOKENS", str(table))
    import importlib
    from pathlib import Path
    from starlette.testclient import TestClient
    from core.control.registry import manifest_errors
    from axcommon.client import ApiClient
    import yaml

    env = control_env
    root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((root / "plugins/resource/worker-pool/manifest.yaml").read_text())
    # Register the production manifest, rather than the simplified T-5 fixture.
    assert not manifest_errors(manifest)
    fixture_manifest = env["root"] / "plugins/resource/worker-pool/manifest.yaml"
    fixture_manifest.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert "worker-pool" in env["service"].register()["registered"]
    pool_app = importlib.import_module("plugins.resource.worker-pool.app")
    pool_jobs = importlib.import_module("plugins.resource.worker-pool.jobs")
    pool_core, _, pool = pool_app.build(store=pool_jobs.JobsStore(str(env["tmp_path"] / "pool.sqlite")), run_heartbeat_loop=False)
    pool_client = TestClient(pool_core, headers={"Authorization": "Bearer test-intake"})
    transport = env["transport"]
    from core.resources.app import build as build_resources, Config as ResourcesConfig
    from core.resources.store import Store as ResourcesStore
    resource_store = ResourcesStore(str(env["tmp_path"] / "resources.sqlite"))
    resource_store.seed([{"id": "vendor/a", "family": "vendor", "kind": "model"},
                         {"id": "other/b", "family": "other", "kind": "model"}])
    from core.resources.core_auth import CoreAuth
    from axcommon.creds import TokenTable, sha256_hex
    resource_app, _, _ = build_resources(ResourcesConfig(ext_dir=str(env["tmp_path"] / "no-extensions")),
                                         store=resource_store, auth=CoreAuth(TokenTable({"control": sha256_hex("limits-test")})), run_heartbeat_loop=False)
    resource_client = TestClient(resource_app)
    def resource_forward(call):
        response = resource_client.request(call.method, call.path + ("?" + call.query if call.query else ""), content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content
    transport.add("GET", "/v1/resources", handler=resource_forward)
    transport.add("GET", "/v1/slots/effective-limits", handler=resource_forward)
    transport.add("PUT", "/v1/slots/effective-limits", handler=resource_forward)
    env["service"].resources_client = ApiClient("http://resources:8080", token="limits-test", transport=transport)

    def forward(call):
        response = pool_client.request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content

    transport.add("POST", "/apply", handler=forward)
    transport.add("GET", "/status", handler=forward)
    values = {"role_candidates": {"research": ["vendor/a", "flash"]},
              "family_cap": {"vendor": 2}, "model_cap": {"vendor/a": 1},
              "model_descriptions": {"vendor/a": "research"}}
    for name, value in values.items():
        assert _issue(env, USER, _set_knob("worker-pool", name, value, issued_by="user")).status_code == 201
    before = pool_client.get("/status").json()
    for name, value in values.items():
        assert before["knobs"][name] == value
    assert pool.selection_knobs().family_cap == {"vendor": 2}
    assert pool.selection_knobs().role_candidates == values["role_candidates"]
    assert pool_client.post("/apply", json={"mode": "run", "knobs": {}, "revision": 999}).status_code == 403
    prior_calls = len([c for c in transport.calls if c.path == "/apply"])
    invalid = [("role_candidates", {"research": ["vendor/a", "vendor/a"]}),
               ("role_candidates", {"unknown": ["flash"]}),
               ("role_candidates", {"research": ["missing"]}),
               ("family_cap", {"missing": 2}), ("model_cap", {"vendor/a": True}),
               ("model_cap", {"vendor/a": -1}),
               ("model_descriptions", {"missing": "x"})]
    for name, value in invalid:
        assert _issue(env, USER, _set_knob("worker-pool", name, value, issued_by="user")).status_code == 400
    assert _issue(env, MGR, _set_knob("worker-pool", "model_descriptions", {"vendor/a": "changed"}, expires_at=_in(30))).status_code == 400
    assert len([c for c in transport.calls if c.path == "/apply"]) == prior_calls
    assert pool_client.get("/status").json() == before
    assert _effective(env, "worker-pool")["knobs"] == before["knobs"]
    newer = _issue(env, USER, _set_knob("worker-pool", "family_cap", {"other": 3}, issued_by="user"))
    assert newer.status_code == 201
    assert pool_client.get("/status").json()["knobs"]["family_cap"] == {"other": 3}
    released = env["core"].post("/v1/commands/" + newer.json()["id"] + "/release",
                                json={"issued_by": "user"}, headers=USER)
    assert released.status_code == 200
    assert pool_client.get("/status").json()["knobs"]["family_cap"] == {"vendor": 2}
    transport.add("GET", "/v1/resources", status=503, json={"error": {"code": "down"}})
    prior_calls = len([c for c in transport.calls if c.path == "/apply"])
    assert _issue(env, USER, _set_knob("worker-pool", "model_cap", {}, issued_by="user")).status_code == 503
    assert len([c for c in transport.calls if c.path == "/apply"]) == prior_calls
    pool.store.close()
    resource_store.close()


def test_worker_pool_recovers_effective_and_rejects_untrusted_apply(control_env, tmp_path, monkeypatch):
    from axcommon.creds import sha256_hex
    table = tmp_path / "pool-core-tokens"
    table.write_text("intake " + sha256_hex("test-intake") + "\n")
    monkeypatch.setenv("AX_CORE_TOKENS", str(table))
    import importlib
    import time
    from urllib.parse import urlsplit
    from starlette.testclient import TestClient
    from axcommon.client import ApiClient

    env = control_env
    response = _issue(env, INS, _set_mode('worker-pool', 'draining', expires_at=_in(20)), 201)
    assert response.status_code == 201

    def transport(method, url, headers, body, timeout):
        parsed = urlsplit(url)
        result = env['core'].request(method, parsed.path, headers=headers, content=body)
        return result.status_code, dict(result.headers), result.content

    mod = importlib.import_module('plugins.resource.worker-pool.app')
    control = ApiClient('http://control', transport=transport)
    config = mod.Config(db=str(tmp_path / 'pool-control.sqlite'))
    for _ in range(2):
        application, _, service = mod.build(config, control=control, run_heartbeat_loop=False)
        with TestClient(application, headers={"Authorization": "Bearer test-intake"}) as client:
            end = time.monotonic() + 3
            while client.get('/status').json()['revision'] < 0 and time.monotonic() < end:
                time.sleep(0.01)
            current = client.get('/status').json()
            assert current['mode'] == 'draining'
            assert client.post('/apply', json={'mode': 'run', 'knobs': {}, 'revision': current['revision'] + 1}).status_code == 403
            assert client.get('/status').json()['mode'] == 'draining'
        service.store.close()


def test_command_exclusion_real_resources_client_resync_and_restart(control_env):
    from axcommon.client import ApiClient
    from core.control.app import create_core_app
    from core.control.selection_exclusions import ExclusionAdapter, ResourcesExclusionClient
    from core.control.service import ControlService
    from core.control.store import Store as ControlStore
    from core.resources.app import build as build_resources, Config
    from core.resources.store import Store
    from starlette.testclient import TestClient
    from tests.fakes.http import FakeTransport

    env = control_env
    store = Store(str(env["tmp_path"] / "exclusions.sqlite"))
    store.seed([{"id": "model-a", "kind": "model", "family": "vendor"},
                {"id": "retired", "kind": "model", "family": "vendor", "status": "retired"}])
    from axcommon.creds import TokenTable, sha256_hex
    from core.resources.core_auth import CoreAuth
    auth = CoreAuth(TokenTable({"control": sha256_hex("test-control-token")}))
    app, _, _ = build_resources(Config(ext_dir=str(env["tmp_path"] / "no-ext")), store=store, auth=auth, run_heartbeat_loop=False)
    resources = TestClient(app)
    transport = FakeTransport()
    online = [False]

    def forward(call):
        if not online[0]:
            return 503, {}, b'{}'
        response = resources.request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content

    transport.add("POST", "/v1/command-exclusions", handler=forward)
    adapter = ExclusionAdapter(ResourcesExclusionClient(ApiClient("http://resources", token="test-control-token", transport=transport)))
    env["service"].exclusion_adapter = adapter
    until = _in(20)
    body = {"target": "worker-pool", "op": "exclude_resource", "args": {"resource_id": "model-a", "until": until},
            "expires_at": until, "on_expiry": "release", "reason": "temporary"}
    assert _issue(env, MGR, body, expect=503).json()["error"]["code"] == "exclusion_unavailable"
    assert not env["service"].list_commands()
    online[0] = True
    first = _issue(env, MGR, body, expect=201).json()
    second = _issue(env, MGR, body, expect=201).json()

    def status():
        rows = resources.get("/v1/resources", params={"kind": "model"}).json()["resources"]
        return {row["id"]: row["status"] for row in rows}

    assert status()["model-a"] == "excluded"
    assert env["core"].post("/v1/sweep", json={}, headers=MGR).status_code == 200
    assert status()["model-a"] == "excluded"
    assert "model-a" not in {row["id"] for row in resources.get("/v1/resources", params={"status": "active"}).json()["resources"]}
    assert "model-a" in {row["id"] for row in resources.get("/v1/resources", params={"status": "excluded"}).json()["resources"]}
    assert env["core"].post("/v1/sweep", json={}, headers=MGR).status_code == 200
    assert env["core"].post("/v1/commands/" + first["id"] + "/release", json={"reason": "done"}, headers=MGR).status_code == 200
    assert status()["model-a"] == "excluded"

    assert resources.post("/v1/resources/model-a/exclude", json={"reason": "other", "until": until}).status_code == 200
    resources.close()
    store.close()
    store = Store(str(env["tmp_path"] / "exclusions.sqlite"))
    app, _, _ = build_resources(Config(ext_dir=str(env["tmp_path"] / "no-ext")), store=store, auth=auth, run_heartbeat_loop=False)
    resources = TestClient(app)
    assert status()["model-a"] == "excluded"
    restarted = ControlService(ControlStore(env["service"].store.path), None, exclusion_adapter=adapter)
    with TestClient(create_core_app(restarted)) as control:
        assert control.post("/v1/sweep", json={}, headers=MGR).status_code == 200
        assert status()["model-a"] == "excluded"
        online[0] = False
        assert control.post("/v1/commands/" + second["id"] + "/release", json={"reason": "done"}, headers=MGR).status_code == 200
        assert status()["model-a"] == "excluded"
        online[0] = True
        assert control.post("/v1/sweep", json={}, headers=MGR).status_code == 200
    assert status()["model-a"] == "excluded"
    assert resources.post("/v1/resources/model-a/include", json={"reason": "other", "until": until}).status_code == 200
    assert status()["model-a"] == "active"
    assert status()["retired"] == "retired"


def test_control_starts_without_exclusion_credential(control_env, tmp_path, monkeypatch, caplog):
    from core.control.__main__ import build
    from starlette.testclient import TestClient

    monkeypatch.setenv("AX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AX_SECRETS_PATH", str(tmp_path / "missing"))
    core, _, service = build()
    assert service.exclusion_adapter is None
    assert "control credential unavailable" in caplog.text
    with TestClient(core) as client:
        assert client.get("/healthz").status_code == 200


# The pool and launcher are exercised through their public HTTP interfaces.
from tests.b6a.test_dispatch_e2e import system, submit, wait_for


@pytest.mark.parametrize("system", ["two_models"], indirect=True)
def test_exclusion_controls_real_job_dispatch(control_env, system):
    from axcommon.client import ApiClient
    from core.control.selection_exclusions import ExclusionAdapter, ResourcesExclusionClient
    from tests.fakes.http import FakeTransport

    env = control_env
    pool, resources, state = system
    transport = FakeTransport()
    online = [False]
    def forward(call):
        if not online[0]:
            return 503, {}, b'{}'
        response = resources.request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content
    transport.add("POST", "/v1/command-exclusions", handler=forward)
    env["service"].exclusion_adapter = ExclusionAdapter(ResourcesExclusionClient(ApiClient("http://resources", token="control-test", transport=transport)))
    until = _in(20)
    body = {"target": "worker-pool", "op": "exclude_resource", "args": {"resource_id": "example", "until": until},
            "expires_at": until, "on_expiry": "release", "reason": "temporary"}
    _issue(env, MGR, body, expect=503)
    online[0] = True
    first = _issue(env, MGR, body, expect=201).json()
    assert env["core"].post("/v1/sweep", json={}, headers=MGR).status_code == 200
    reserve = resources.post('/v1/slots/reserve', json={'reservation_id': 'direct', 'model': 'example/model'}, headers={'Authorization': 'Bearer pool-test'})
    assert reserve.status_code == 409
    online[0] = False
    other_body = {**body, 'args': {'resource_id': 'other/model', 'until': until}}
    _issue(env, MGR, other_body, expect=503)
    jid = submit(pool)
    wait_for(lambda: pool.get('/v1/jobs/' + jid).json()['state'] == 'running')
    assert next(call for call in state['calls'] if call['job_id'] == jid)['model'] == 'other/model'
    online[0] = True
    _issue(env, MGR, other_body, expect=201)
    assert env['core'].post('/v1/sweep', json={}, headers=MGR).status_code == 200
    jid2 = submit(pool)
    wait_for(lambda: pool.get('/v1/jobs/' + jid2).json()['reason'] == 'no_eligible_candidates')
    assert not any(call['job_id'] == jid2 for call in state['calls'])
    assert env['core'].post('/v1/commands/' + first['id'] + '/release', json={'reason': 'done'}, headers=MGR).status_code == 200
    wait_for(lambda: pool.get('/v1/jobs/' + jid2).json()['state'] == 'running')
    assert next(call for call in state['calls'] if call['job_id'] == jid2)['model'] == 'example/model'


def test_exclusion_after_reservation_and_cancel_survives_resync(control_env, system):
    import threading
    from axcommon.client import ApiClient
    from core.control.selection_exclusions import ExclusionAdapter, ResourcesExclusionClient
    from tests.fakes.http import FakeTransport

    env = control_env
    pool, resources, state = system
    transport = FakeTransport()
    online = [True]
    def forward(call):
        if not online[0]:
            return 503, {}, b'{}'
        response = resources.request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content
    transport.add('POST', '/v1/command-exclusions', handler=forward)
    env['service'].exclusion_adapter = ExclusionAdapter(ResourcesExclusionClient(ApiClient('http://resources', token='control-test', transport=transport)))
    state['reserve_entered'] = threading.Event()
    state['reserve_continue'] = threading.Event()
    jid = submit(pool)
    assert state['reserve_entered'].wait(8)
    until = _in(20)
    body = {'target': 'worker-pool', 'op': 'exclude_resource', 'args': {'resource_id': 'example/model', 'until': until},
            'expires_at': until, 'on_expiry': 'release', 'reason': 'temporary'}
    first = _issue(env, MGR, body, expect=201).json()
    second = _issue(env, MGR, body, expect=201).json()
    state['reserve_continue'].set()
    wait_for(lambda: pool.get('/v1/jobs/' + jid).json()['reason'] == 'model_unavailable')
    assert not state['calls']
    assert pool.post('/v1/jobs/' + jid + '/cancel').status_code in (200, 202)
    wait_for(lambda: pool.get('/v1/jobs/' + jid).json()['state'] == 'cancelled')
    assert env['core'].post('/v1/commands/' + first['id'] + '/release', json={'reason': 'done'}, headers=MGR).status_code == 200
    assert resources.get('/v1/resources').json()['resources'][0]['status'] == 'excluded'
    online[0] = False
    assert env['core'].post('/v1/commands/' + second['id'] + '/release', json={'reason': 'done'}, headers=MGR).status_code == 200
    online[0] = True
    assert env['core'].post('/v1/sweep', json={}, headers=MGR).status_code == 200
    assert resources.get('/v1/resources').json()['resources'][0]['status'] == 'active'
    assert pool.get('/v1/jobs/' + jid).json()['state'] == 'cancelled'
    assert not state['calls']


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_exclusion_reassigns_held_slot(control_env, system):
    import threading
    from axcommon.client import ApiClient
    from core.control.selection_exclusions import ExclusionAdapter, ResourcesExclusionClient
    from tests.fakes.http import FakeTransport
    env = control_env
    pool, resources, state = system
    transport = FakeTransport()
    def forward(call):
        response = resources.request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content
    transport.add('POST', '/v1/command-exclusions', handler=forward)
    env['service'].exclusion_adapter = ExclusionAdapter(ResourcesExclusionClient(ApiClient('http://resources', token='control-test', transport=transport)))
    state['reserve_entered'], state['reserve_continue'] = threading.Event(), threading.Event()
    jid = submit(pool)
    assert state['reserve_entered'].wait(8)
    until = _in(20)
    body = {'target': 'worker-pool', 'op': 'exclude_resource', 'args': {'resource_id': 'example/model', 'until': until},
            'expires_at': until, 'on_expiry': 'release', 'reason': 'temporary'}
    _issue(env, MGR, body, expect=201)
    state['reserve_continue'].set()
    wait_for(lambda: pool.get('/v1/jobs/' + jid).json()['state'] == 'running')
    assert next(call for call in state['calls'] if call['job_id'] == jid)['model'] == 'other/model'
    slots = resources.get('/v1/slots').json()['models']
    assert slots['example/model']['used'] == 0
    assert slots['other/model']['used'] == 1
    assert pool.post('/v1/jobs/' + jid + '/cancel').status_code in (200, 202)
    wait_for(lambda: pool.get('/v1/jobs/' + jid).json()['state'] == 'cancelled')
    wait_for(lambda: resources.get('/v1/slots').json()['models']['other/model']['used'] == 0)


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
@pytest.mark.parametrize('sync_mode', ['register', 'resync'])
def test_exclusion_waits_for_start_already_in_progress(control_env, system, sync_mode):
    import threading
    from axcommon.client import ApiClient
    from core.control.selection_exclusions import ExclusionAdapter, ResourcesExclusionClient
    from tests.fakes.http import FakeTransport
    env = control_env
    pool, resources, state = system
    transport = FakeTransport()
    online = [True]
    def forward(call):
        if not online[0]:
            return 503, {}, b'{}'
        response = resources.request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content
    transport.add('POST', '/v1/command-exclusions', handler=forward)
    env['service'].exclusion_adapter = ExclusionAdapter(ResourcesExclusionClient(ApiClient('http://resources', token='control-test', transport=transport)))
    until = _in(20)
    body = {'target': 'worker-pool', 'op': 'exclude_resource', 'args': {'resource_id': 'example/model', 'until': until},
            'expires_at': until, 'on_expiry': 'release', 'reason': 'temporary'}
    if sync_mode == 'resync':
        command = _issue(env, MGR, body, expect=201).json()
        response = resources.post('/v1/command-exclusions', headers={'Authorization': 'Bearer control-test'},
                                  json={'command_id': command['id'], 'resource_id': 'example/model', 'active': False})
        assert response.status_code == 200
    state['start_entered'], state['start_continue'] = threading.Event(), threading.Event()
    jid = submit(pool)
    assert state['start_entered'].wait(8)
    finished = threading.Event()
    results = []
    def issue():
        results.append(_issue(env, MGR, body) if sync_mode == 'register' else env['core'].post('/v1/sweep', json={}, headers=MGR))
        finished.set()
    thread = threading.Thread(target=issue)
    thread.start()
    assert not finished.wait(.3)
    state['start_continue'].set()
    assert finished.wait(8)
    thread.join()
    assert results[0].status_code == (201 if sync_mode == 'register' else 200)
    assert next(call for call in state['calls'] if call['job_id'] == jid)['model'] == 'example/model'
    assert resources.get('/v1/resources').json()['resources'][0]['status'] == 'excluded'
    jid2 = submit(pool)
    wait_for(lambda: pool.get('/v1/jobs/' + jid2).json()['state'] == 'running')
    assert next(call for call in state['calls'] if call['job_id'] == jid2)['model'] == 'other/model'


def test_reserving_job_releases_slot_until_exclusion_ends(control_env, system):
    import threading
    from axcommon.client import ApiClient
    from core.control.selection_exclusions import ExclusionAdapter, ResourcesExclusionClient
    from tests.fakes.http import FakeTransport
    env = control_env
    pool, resources, state = system
    transport = FakeTransport()
    def forward(call):
        response = resources.request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content
    transport.add('POST', '/v1/command-exclusions', handler=forward)
    env['service'].exclusion_adapter = ExclusionAdapter(ResourcesExclusionClient(ApiClient('http://resources', token='control-test', transport=transport)))
    state['reserve_entered'], state['reserve_continue'] = threading.Event(), threading.Event()
    jid = submit(pool)
    assert state['reserve_entered'].wait(8)
    until = _in(20)
    body = {'target': 'worker-pool', 'op': 'exclude_resource', 'args': {'resource_id': 'example/model', 'until': until},
            'expires_at': until, 'on_expiry': 'release', 'reason': 'temporary'}
    command = _issue(env, MGR, body, expect=201).json()
    state['reserve_continue'].set()
    wait_for(lambda: pool.get('/v1/jobs/' + jid).json()['reason'] == 'no_eligible_candidates')
    assert resources.get('/v1/slots').json()['models']['example/model']['used'] == 0
    assert not state['calls']
    assert env['core'].post('/v1/commands/' + command['id'] + '/release', json={'reason': 'done'}, headers=MGR).status_code == 200
    wait_for(lambda: pool.get('/v1/jobs/' + jid).json()['state'] == 'running')
    assert resources.get('/v1/slots').json()['models']['example/model']['used'] == 1


@pytest.mark.parametrize('system', ['two_models'], indirect=True)
def test_exclusion_requires_sync_and_blocks_only_new_target_reservations(control_env, system):
    import time
    from axcommon.client import ApiClient
    from core.control.selection_exclusions import ExclusionAdapter, ResourcesExclusionClient
    from tests.fakes.http import FakeTransport

    env = control_env
    _, resources, _ = system
    until = _in(20)
    body = {'target': 'worker-pool', 'op': 'exclude_resource',
            'args': {'resource_id': 'example/model', 'until': until},
            'expires_at': until, 'on_expiry': 'release', 'reason': 'temporary'}
    env['service'].exclusion_adapter = None
    assert _issue(env, MGR, body, expect=503).json()['error']['code'] == 'exclusion_unavailable'
    assert not env['service'].list_commands()

    transport = FakeTransport()
    online = [False]
    def forward(call):
        if not online[0]:
            return 503, {}, b'{}'
        response = resources.request(call.method, call.path, content=call.body, headers=call.headers)
        return response.status_code, dict(response.headers), response.content
    transport.add('POST', '/v1/command-exclusions', handler=forward)
    env['service'].exclusion_adapter = ExclusionAdapter(ResourcesExclusionClient(
        ApiClient('http://resources', token='control-test', transport=transport)))
    assert _issue(env, MGR, body, expect=503).json()['error']['code'] == 'exclusion_unavailable'
    assert not env['service'].list_commands()
    assert resources.put('/v1/slots/effective-limits', headers={'Authorization': 'Bearer control-test'},
        json={'revision': 2, 'expires_at': time.time() + 3600,
              'models': {'example/model': 2, 'other/model': 1}, 'families': {'example': 2, 'other': 1}}).status_code == 200
    headers = {'Authorization': 'Bearer pool-test'}
    def reserve(ident, model):
        return resources.post('/v1/slots/reserve', headers=headers, json={'reservation_id': ident, 'model': model})
    assert reserve('held-job', 'example/model').status_code == 200
    online[0] = True
    command = _issue(env, MGR, body, expect=201).json()
    blocked = reserve('blocked-job', 'example/model')
    assert blocked.status_code == 409 and blocked.json()['reason'] == 'model_excluded'
    assert resources.get('/v1/slots').json()['models']['example/model']['used'] == 1
    assert reserve('other-job', 'other/model').status_code == 200
    assert env['core'].post('/v1/commands/' + command['id'] + '/release',
                            json={'reason': 'done'}, headers=MGR).status_code == 200
    assert reserve('after-release', 'example/model').status_code == 200

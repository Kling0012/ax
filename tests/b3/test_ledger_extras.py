"""B3 の追加の確認: 判断の記録（S-9.6、D-8）と目標・約束・予定（S-9.4、S-9.5）。

T-4/T-183/T-5 の対象外だが、この束が実装した部分の受け入れ（段 B3 の「合否の試験」の補助）。
"""
from __future__ import annotations

from axcommon import timeutil

from tests.b3.conftest import TOK_HQ, TOK_INTAKE

HDR = {"Authorization": "Bearer " + TOK_INTAKE}
HQ = {**HDR, "X-AX-Via": "hq"}


# ---- 判断の記録（S-9.6、D-8）----


def test_decision_is_recorded_and_supersedes(ledger_env):
    env = ledger_env
    d1 = {"kind": "preference", "project": "dreamforge", "title": "バランスは自分で決める",
          "body": "ドロップ率はワーカーが決めて報告する", "reason": "利用者の希望", "basis": "user_said",
          "quote": "数字は任せる"}
    r = env["core"].post("/v1/decisions", json=d1, headers=HQ)
    assert r.status_code == 201, r.text
    first = r.json()
    # hq の受け口からの記録だけ attested が true になる（D-8: 本文の値は無視する）
    assert first["attested"] is True and first["status"] == "active"
    # 司令塔の受け口からは attested が false
    d2 = {**d1, "title": "更新", "basis": "commander", "supersedes": first["id"]}
    r = env["core"].post("/v1/decisions", json=d2, headers={**HDR, "X-AX-Via": "local"})
    assert r.status_code == 201, r.text
    assert r.json()["attested"] is False
    # 前の行は superseded になる
    got = env["core"].get(f"/v1/decisions/{first['id']}").json()
    assert got["decision"]["status"] == "superseded"
    assert got["superseded_by"] == [r.json()["id"]]
    # 一覧は status=active が既定
    active = env["core"].get("/v1/decisions").json()["decisions"]
    assert [d["id"] for d in active] == [r.json()["id"]]


def test_decision_requires_quote_for_user_said_and_scans_secrets(ledger_env):
    env = ledger_env
    r = env["core"].post("/v1/decisions", json={"kind": "preference", "title": "無引用",
                                                "body": "本文", "basis": "user_said"}, headers=HQ)
    assert r.status_code == 422
    r = env["core"].post("/v1/decisions", json={"kind": "decision", "title": "秘密",
                                                "body": "FICTIONAL の鍵で運用する", "basis": "design"}, headers=HQ)
    assert r.status_code == 422 and r.json()["error"]["code"] == "secret_detected"


# ---- 目標・約束・予定（S-9.4、S-9.5）----


def test_goal_promise_schedule_lifecycle(ledger_env):
    env = ledger_env
    r = env["core"].post("/v1/goals", json={"id": "g1", "body": "v3.9 を出す"}, headers=HDR)
    assert r.status_code == 201 and r.json()["state"] == "active"
    # 遷移には理由が要る
    r = env["core"].post("/v1/goals/g1/transition", json={"to": "done"}, headers=HDR)
    assert r.status_code == 422
    r = env["core"].post("/v1/goals/g1/transition", json={"to": "done", "reason": "リリースした", "version": 1}, headers=HDR)
    assert r.status_code == 200 and r.json()["state"] == "done"
    # 表にない遷移は拒否
    r = env["core"].post("/v1/goals/g1/transition", json={"to": "cancelled", "reason": "無い状態"}, headers=HDR)
    assert r.status_code == 409

    # 約束は関連 ToDo と期限が必須
    r = env["core"].post("/v1/promises", json={"id": "p1", "body": "夕方までに報告"}, headers=HDR)
    assert r.status_code == 422
    r = env["core"].post("/v1/promises", json={"id": "p1", "body": "夕方までに報告", "todo_id": "t-x",
                                               "due_at": timeutil.add_seconds(timeutil.now_ts(), 3600)}, headers=HDR)
    assert r.status_code == 201, r.text
    # 未完了の約束は ToDo と期限で照会できる
    got = env["core"].get("/v1/promises", params={"todo_id": "t-x", "state": "open"}).json()["promises"]
    assert [p["id"] for p in got] == ["p1"]

    # 予定は終了が開始以降
    start = timeutil.now_ts()
    r = env["core"].post("/v1/schedules", json={"id": "s1", "body": "点検", "start_at": start,
                                                "end_at": timeutil.add_seconds(start, -10)}, headers=HDR)
    assert r.status_code == 422
    r = env["core"].post("/v1/schedules", json={"id": "s1", "body": "点検", "start_at": start,
                                                "end_at": timeutil.add_seconds(start, 600)}, headers=HDR)
    assert r.status_code == 201
    r = env["core"].post("/v1/schedules/s1/transition", json={"to": "done", "reason": "終わった", "version": 1}, headers=HDR)
    assert r.status_code == 200


def test_item_patch_checks_version(ledger_env):
    """更新・遷移は対象の版を照合し、古い版による上書きを拒否する（S-9.5）。"""
    env = ledger_env
    env["core"].post("/v1/goals", json={"id": "g9", "body": "旧本文"}, headers=HDR)
    r = env["core"].patch("/v1/goals/g9", json={"body": "新本文", "version": 7}, headers=HDR)
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_version"
    r = env["core"].patch("/v1/goals/g9", json={"body": "新本文", "version": 1}, headers=HDR)
    assert r.status_code == 200 and r.json()["body"] == "新本文"


def test_item_versions_advance_and_terminal_states_cannot_switch(ledger_env):
    env = ledger_env
    created = env["core"].post("/v1/goals", json={"id": "versions", "body": "first"}, headers=HDR).json()
    current = created.get("version", 1)
    updated = env["core"].patch("/v1/goals/versions", json={"body": "second", "version": current}, headers=HDR)
    assert updated.status_code == 200
    assert updated.json()["version"] == current + 1
    stale = env["core"].patch("/v1/goals/versions", json={"body": "stale", "version": current}, headers=HDR)
    assert stale.status_code == 409
    done = env["core"].post("/v1/goals/versions/transition", json={"to": "done", "reason": "finished", "version": current + 1}, headers=HDR)
    assert done.status_code == 200
    invalid = env["core"].post("/v1/goals/versions/transition", json={"to": "dropped", "reason": "switch"}, headers=HDR)
    assert invalid.status_code == 409


def test_item_identity_cannot_be_changed_by_patch(ledger_env):
    env = ledger_env
    env["core"].post("/v1/goals", json={"id": "identity", "body": "first"}, headers=HDR)
    response = env["core"].patch("/v1/goals/identity", json={"id": "other", "body": "changed"}, headers=HDR)
    assert response.status_code == 422
    assert env["service"].get_item("goal", "identity")["body"] == "first"
    assert env["service"].get_item("goal", "other") is None


def test_schedules_filter_event_times_and_reject_invalid_dates(ledger_env):
    env = ledger_env
    created = env["core"].post("/v1/schedules", json={
        "id": "future-schedule", "body": "scheduled", "start_at": "2026-12-01T09:00:00+09:00",
        "end_at": "2026-12-01T10:00:00+09:00"}, headers=HDR)
    assert created.status_code == 201
    listed = env["core"].get("/v1/schedules", params={
        "from": "2026-12-01T00:30:00Z", "to": "2026-12-01T00:45:00Z"})
    assert [item["id"] for item in listed.json()["schedules"]] == ["future-schedule"]
    invalid = env["core"].patch("/v1/schedules/future-schedule", json={"end_at": "2026-02-30T00:00:00Z", "version": 1}, headers=HDR)
    assert invalid.status_code == 422
    assert env["service"].get_item("schedule", "future-schedule")["end_at"] == "2026-12-01T10:00:00+09:00"


def test_todo_list_filters(ledger_env):
    env = ledger_env
    env["core"].post("/v1/todos", json={"id": "f1", "title": "期限付き", "state": "todo", "owner": "system",
                                        "due_at": timeutil.add_seconds(timeutil.now_ts(), -60),
                                        "project": "dreamforge", "subloop": "issue-automation"}, headers=HDR)
    env["core"].post("/v1/todos", json={"id": "f2", "title": "未期限", "state": "todo", "owner": "system"}, headers=HDR)
    assert [t["id"] for t in env["core"].get("/v1/todos", params={"due": "1"}).json()["todos"]] == ["f1"]
    assert [t["id"] for t in env["core"].get("/v1/todos", params={"project": "dreamforge"}).json()["todos"]] == ["f1"]
    assert [t["id"] for t in env["core"].get("/v1/todos", params={"state": "todo"}).json()["todos"]] == ["f1", "f2"]


def test_ledger_binds_each_mouth_only_to_its_network():
    from core.ledger.__main__ import resolve_binds

    addresses = ["127.0.0.1", "192.0.2.7", "198.51.100.7"]
    core, work = resolve_binds({}, addresses)
    assert core == ["192.0.2.7", "127.0.0.1"]
    assert work == ["198.51.100.7"]
    assert resolve_binds({}, ["127.0.0.1"]) == (["127.0.0.1"], [])
    core, work = resolve_binds({"AX_LEDGER_CORE_BIND": "192.0.2.1", "AX_LEDGER_WORK_BIND": "192.0.2.2"}, [])
    assert core == ["192.0.2.1", "127.0.0.1"] and work == ["192.0.2.2"]


def test_token_table_created_after_startup_is_reloaded(ledger_env, monkeypatch):
    from core.ledger import workauth
    from tests.b3.conftest import TOK_HIBI, write_table

    path = ledger_env["tmp_path"] / "late-plugin-table"
    monkeypatch.setenv("AX_PLUGIN_TOKENS", str(path))
    table = workauth.plugin_table()
    assert workauth.identify_plugin(table, "Bearer " + TOK_HIBI) is None
    write_table(path, {"hibi-state": TOK_HIBI})
    workauth.reload_if_changed(table)
    assert workauth.identify_plugin(table, "Bearer " + TOK_HIBI) == "hibi-state"


def test_non_object_request_bodies_are_rejected(ledger_env):
    for body in (None, [], "text", 4):
        response = ledger_env["core"].post("/v1/todos", json=body, headers=HDR)
        assert response.status_code == 422


def test_nested_store_transaction_rolls_back_all_writes(ledger_env):
    import pytest

    store = ledger_env["service"].store
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.set_cursor("outer", "first")
            with store.transaction():
                store.set_cursor("inner", "second")
            raise RuntimeError("injected failure")
    assert store.get_cursor("outer") is None
    assert store.get_cursor("inner") is None


def test_watch_reads_item_state_with_cursor(ledger_env):
    """D-8: 読み取りの位置は台帳が持ち、再起動後は続きから読む。"""
    env = ledger_env
    from core.ledger.watch import LedgerWatcher
    from tests.fakes.http import FakeTransport

    t = FakeTransport()
    t.add("GET", "/v1/events", handler=lambda c: (200, {}, __import__("json").dumps(
        {"events": [{"seq": 5, "id": "e5", "data": {"todo_id": "w1", "state": "missed"}},
                    {"seq": 6, "id": "e6", "data": {"todo_id": "w1", "state": "ejected", "reason": "外へ出した"}}],
         "next_after": 6}).encode()))
    env["core"].post("/v1/todos", json={"id": "w1", "title": "実行", "state": "running", "owner": "system"},
                     headers=HDR)
    watcher = LedgerWatcher(env["service"], __import__("axcommon.client", fromlist=["TimelineClient"]).TimelineClient(
        "http://timeline:8080", transport=t))
    out = watcher.step()
    assert out["handled"] == ["w1"] and out["after"] == "6"
    assert env["service"].store.get_cursor("item.state") == "6"
    assert env["core"].get("/v1/todos/w1", headers=HDR).json()["state"] == "parked"
    # 2回目は続き（after=6）から読む
    watcher.step()
    assert [c for c in t.calls if c.method == "GET"][-1].query.startswith("after=6")


def test_item_updates_require_version_and_transitions_reject_stale_version(ledger_env):
    client = ledger_env["core"]
    client.post("/v1/goals", json={"id": "required-version", "body": "initial"}, headers=HDR)
    assert client.patch("/v1/goals/required-version", json={"body": "changed"}, headers=HDR).status_code == 422
    transition = {"to": "done", "reason": "finished"}
    assert client.post("/v1/goals/required-version/transition", json=transition, headers=HDR).status_code == 422
    assert client.post("/v1/goals/required-version/transition", json={**transition, "version": 7}, headers=HDR).status_code == 409
    assert ledger_env["service"].get_item("goal", "required-version")["state"] == "active"

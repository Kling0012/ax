"""T-4 台帳: 表にない遷移の拒否、理由のない waiting_user の拒否、依存先の parked で依存する件が parked になる。

S-9.2、S-9.3、D-8。中核の口（8080 相当）を TestClient で試す（ネットワークを使わない）。
追加で S-9.2 の user_only の既定（T-212 の台帳側の部分）と item.state の反映（S-9.3、D-8）も確かめる。
"""
from __future__ import annotations

from axcommon import timeutil

from tests.b3.conftest import TOK_INTAKE

TOK_INTAKE_HDR = {"Authorization": "Bearer " + TOK_INTAKE}


def _create(env, todo):
    r = env["core"].post("/v1/todos", json=todo, headers=TOK_INTAKE_HDR)
    assert r.status_code == 201, r.text
    return r.json()


def _transition(env, todo_id, body, expect=None):
    r = env["core"].post(f"/v1/todos/{todo_id}/transition", json=body, headers=TOK_INTAKE_HDR)
    if expect is not None:
        assert r.status_code == expect, f"{body} -> {r.status_code} {r.text}"
    return r


def _kinds(env):
    return [c.json["kind"] for c in env["transport"].calls if c.method == "POST"]


# ---- 表にない遷移の拒否（S-9.2、D-8 の 409）----


def test_t4_invalid_transitions_are_rejected_with_409(ledger_env):
    env = ledger_env
    t = _create(env, {"id": "t-illegal", "title": "試験", "state": "todo", "owner": "system"})
    # todo → done/review は表にない
    _transition(env, "t-illegal", {"to": "done", "reason": "飛び越え"}, expect=409)
    _transition(env, "t-illegal", {"to": "review", "reason": "飛び越え"}, expect=409)
    _transition(env, "t-illegal", {"to": "running"}, expect=200)
    # running → todo は表にある（戻し）
    _transition(env, "t-illegal", {"to": "todo"}, expect=200)
    _transition(env, "t-illegal", {"to": "running"}, expect=200)
    _transition(env, "t-illegal", {"to": "review"}, expect=200)
    # review → running は表にない
    _transition(env, "t-illegal", {"to": "running"}, expect=409)
    _transition(env, "t-illegal", {"to": "done"}, expect=200)
    # done からはどこへも行けない
    _transition(env, "t-illegal", {"to": "todo"}, expect=409)
    _transition(env, "t-illegal", {"to": "running"}, expect=409)
    body = env["core"].get("/v1/todos/t-illegal", headers=TOK_INTAKE_HDR).json()
    assert body["state"] == "done"


def test_t4_parked_requires_reason_and_check_at(ledger_env):
    env = ledger_env
    _create(env, {"id": "t-park", "title": "保留", "state": "running", "owner": "system"})
    _transition(env, "t-park", {"to": "parked"}, expect=422)  # reason が無い
    _transition(env, "t-park", {"to": "parked", "reason": "外へ出した"}, expect=422)  # check_at が無い
    r = _transition(env, "t-park", {"to": "parked", "reason": "外へ出した",
                                    "check_at": timeutil.add_seconds(timeutil.now_ts(), 3600)}, expect=200)
    assert r.json()["reason"] == "外へ出した"


# ---- 理由のない waiting_user の拒否（S-9.2、T-4）----


def test_t4_waiting_user_without_reason_is_rejected(ledger_env):
    env = ledger_env
    t = _create(env, {"id": "t-wait", "title": "質問", "state": "running", "owner": "system"})
    # 理由の種類が決まっていない（自由文しか無い）
    r = _transition(env, "t-wait", {"to": "waiting_user", "reason": "利用者に聞きたい"})
    assert r.status_code == 422, r.text
    # カード（質問）が無い
    r = _transition(env, "t-wait", {"to": "waiting_user", "reason": "preference"})
    assert r.status_code == 422, r.text
    # 作成時に waiting_user を理由なしで作る
    r = env["core"].post("/v1/todos", json={"id": "t-wait2", "title": "質問", "state": "waiting_user",
                                            "owner": "user", "card": {"question": "どちらにする？"}}, headers=TOK_INTAKE_HDR)
    assert r.status_code == 422, r.text


def test_waiting_user_with_card_and_reason_is_accepted_and_user_only_follows_reason(ledger_env):
    """S-9.2 の user_only の既定は理由から台帳が決める（D-8。T-212 の台帳側の部分）。"""
    env = ledger_env
    t = _create(env, {"id": "t-card", "title": "承認", "state": "running", "owner": "system",
                      "card": {"question": "戻しにくい操作をしてよいか", "options": ["はい", "いいえ"]}})
    r = _transition(env, "t-card", {"to": "waiting_user", "reason": "irreversible"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_only"] is True
    assert body["card"]["waiting_reason"] == "irreversible"
    # irreversible で user_only: false を作ろうとしても 422
    r = env["core"].post("/v1/todos", json={
        "id": "t-card2", "title": "承認", "state": "waiting_user", "owner": "user", "user_only": False,
        "card": {"question": "公開してよいか", "waiting_reason": "publish"}}, headers=TOK_INTAKE_HDR)
    assert r.status_code == 422
    # preference は既定 false。作る側が true にしてよい
    t3 = _create(env, {"id": "t-card3", "title": "好み", "state": "waiting_user", "owner": "user", "user_only": True,
                       "card": {"question": "どちらの見た目にする？", "waiting_reason": "preference"}})
    assert t3["user_only"] is True
    t4 = _create(env, {"id": "t-card4", "title": "好み", "state": "waiting_user", "owner": "user",
                       "card": {"question": "どちらの見た目にする？", "waiting_reason": "preference"}})
    assert t4["user_only"] is False


def test_user_only_card_rejects_relay_answer_and_non_hq_completion(ledger_env):
    """S-9.2: user_only のカードは中継の回答と hq 以外の完了・保留・中止を拒否する（台帳側の検査）。"""
    from tests.b3.conftest import TOK_HQ

    env = ledger_env
    _create(env, {"id": "t-uo", "title": "承認", "state": "waiting_user", "owner": "user",
                  "card": {"question": "支払ってよいか", "waiting_reason": "money"}})
    r = env["core"].post("/v1/todos/t-uo/answer",
                         json={"answer": "はい", "answer_source": "relay", "quote": "利用者: はい"},
                         headers=TOK_INTAKE_HDR)
    assert r.status_code == 403 and r.json()["error"]["code"] == "user_only_card"
    for to, reason in (("done", "完了"), ("parked", "保留"), ("dropped", "中止")):
        r = env["core"].post("/v1/todos/t-uo/transition", json={"to": to, "reason": reason,
                                                                "check_at": timeutil.now_ts()},
                             headers={**TOK_INTAKE_HDR, "X-AX-Via": "local"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "user_only_card", r.text
    # hq の受け口からは通る
    r = env["core"].post("/v1/todos/t-uo/answer", json={"answer": "はい"},
                         headers={**TOK_INTAKE_HDR, "X-AX-Via": "hq"})
    assert r.status_code == 200, r.text
    assert r.json()["card"]["answer"]["answer_source"] == "hq"
    assert r.json()["state"] == "todo"


def test_relay_answer_with_quote_is_provisional_for_non_user_only_card(ledger_env):
    """S-9.2: physical/preference/range_review は中継可。quote が空なら拒否。版が違えば拒否。"""
    env = ledger_env
    _create(env, {"id": "t-relay", "title": "好み", "state": "waiting_user", "owner": "user",
                  "card": {"question": "どちら？", "waiting_reason": "preference", "version": 1}})
    r = env["core"].post("/v1/todos/t-relay/answer",
                         json={"answer": "A", "answer_source": "relay", "quote": "", "version": 1}, headers=TOK_INTAKE_HDR)
    assert r.status_code == 422
    r = env["core"].post("/v1/todos/t-relay/answer",
                         json={"answer": "A", "answer_source": "relay", "quote": "利用者: A で", "version": 2},
                         headers=TOK_INTAKE_HDR)
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_mismatch"
    r = env["core"].post("/v1/todos/t-relay/answer",
                         json={"answer": "A", "answer_source": "relay", "quote": "利用者: A で", "version": 1},
                         headers=TOK_INTAKE_HDR)
    assert r.status_code == 200, r.text
    assert r.json()["card"]["answer"]["answer_source"] == "relay"
    # 回答済みのカードへの再回答は拒否
    r = env["core"].post("/v1/todos/t-relay/answer",
                         json={"answer": "B", "answer_source": "hq", "quote": "", "version": 1}, headers=TOK_INTAKE_HDR)
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_answered"


def test_card_user_only_is_preserved_and_cannot_be_overridden(ledger_env):
    env = ledger_env
    card = {"question": "choice", "waiting_reason": "preference", "user_only": True}
    todo = _create(env, {"id": "card-flag", "title": "choice", "state": "waiting_user",
                         "owner": "user", "card": card})
    assert todo["user_only"] is True and todo["card"]["user_only"] is True
    response = env["core"].post("/v1/todos", json={
        "id": "card-false", "title": "approval", "state": "waiting_user", "owner": "user",
        "card": {"question": "approve", "waiting_reason": "money", "user_only": False}},
        headers=TOK_INTAKE_HDR)
    assert response.status_code == 422


def test_user_only_answer_requires_hq_origin(ledger_env):
    _create(ledger_env, {"id": "origin-card", "title": "approval", "state": "waiting_user", "owner": "user",
                         "card": {"question": "approve", "waiting_reason": "money"}})
    response = ledger_env["core"].post("/v1/todos/origin-card/answer",
        json={"answer": "yes", "answer_source": "hq"},
        headers={**TOK_INTAKE_HDR, "X-AX-Via": "local"})
    assert response.status_code == 403
    assert ledger_env["service"].get_todo("origin-card")["state"] == "waiting_user"


def test_ejected_event_cascades_dependencies_and_keeps_run_reference(ledger_env):
    env = ledger_env
    _create(env, {"id": "event-parent", "title": "parent", "state": "running", "owner": "system"})
    _create(env, {"id": "event-child", "title": "child", "state": "todo", "owner": "system",
                  "depends_on": ["event-parent"]})
    env["service"].handle_item_events([{"run_id": "run-new", "data": {
        "todo_id": "event-parent", "item_id": "item-new", "state": "ejected", "reason": "retry"}}])
    assert env["service"].get_todo("event-child")["state"] == "parked"
    assert env["service"].get_todo("event-parent")["source"]["run_id"] == "run-new"


def test_repeated_create_does_not_overwrite_existing_todo(ledger_env):
    _create(ledger_env, {"id": "same-id", "title": "first", "state": "todo", "owner": "system"})
    response = ledger_env["core"].post("/v1/todos", json={
        "id": "same-id", "title": "replacement", "state": "done", "owner": "system"}, headers=TOK_INTAKE_HDR)
    assert response.status_code == 409
    assert ledger_env["service"].get_todo("same-id")["state"] == "todo"


# ---- 依存（S-9.2、D-8）----


def test_t4_parking_a_dependency_parks_dependents(ledger_env):
    env = ledger_env
    _create(env, {"id": "dep-a", "title": "先", "state": "running", "owner": "system"})
    _create(env, {"id": "dep-b", "title": "後", "state": "running", "owner": "system", "depends_on": ["dep-a"]})
    _create(env, {"id": "dep-c", "title": "後2", "state": "todo", "owner": "system", "depends_on": ["dep-b"]})
    _transition(env, "dep-a", {"to": "parked", "reason": "外へ出した",
                               "check_at": timeutil.add_seconds(timeutil.now_ts(), 3600)}, expect=200)
    b = env["core"].get("/v1/todos/dep-b", headers=TOK_INTAKE_HDR).json()
    assert b["state"] == "parked" and b["reason"] == "dependency" and b["check_at"]
    # 連鎖: 依存の依存も parked になる（D-8 の deps の逆引き）
    c = env["core"].get("/v1/todos/dep-c", headers=TOK_INTAKE_HDR).json()
    assert c["state"] == "parked" and c["reason"] == "dependency"
    # 依存先が dropped でも同じ
    _create(env, {"id": "dep-d", "title": "後3", "state": "todo", "owner": "system", "depends_on": ["dep-a"]})
    _transition(env, "dep-a", {"to": "dropped", "reason": "不要"}, expect=200)
    d = env["core"].get("/v1/todos/dep-d", headers=TOK_INTAKE_HDR).json()
    assert d["state"] == "parked" and d["reason"] == "dependency"
    # parked になった依存するToDoの事象 task.state が書かれている
    kinds = [(c2.json["kind"], c2.json["subject"]) for c2 in env["transport"].calls if c2.method == "POST"]
    assert any(k == "task.state" and s == "dep-b" for k, s in kinds)


# ---- 追加: item.state の反映と parked の走査（S-9.3、D-8）----


def test_item_state_events_move_the_todo(ledger_env):
    env = ledger_env
    _create(env, {"id": "item-1", "title": "実装", "state": "running", "owner": "system", "item_kind": "work",
                  "source": {"run_id": "r1", "item_id": "i1"}})
    handled = env["service"].handle_item_events([
        {"data": {"todo_id": "item-1", "state": "completed", "item_id": "i1", "artifacts": ["feature/x"]}}])
    assert handled == ["item-1"]
    t = env["core"].get("/v1/todos/item-1", headers=TOK_INTAKE_HDR).json()
    assert t["state"] == "review" and "feature/x" in t["artifacts"]
    # kind: work 以外は触らない
    _create(env, {"id": "item-2", "title": "組立", "state": "running", "owner": "system", "item_kind": "subloop_create"})
    env["service"].handle_item_events([{"data": {"todo_id": "item-2", "state": "completed"}}])
    assert env["core"].get("/v1/todos/item-2", headers=TOK_INTAKE_HDR).json()["state"] == "running"
    # ejected は running から parked（check_at は今＋1日）
    env["service"].handle_item_events([{"data": {"todo_id": "item-2", "state": "ejected", "reason": "隔離が無い"}}])
    t2 = env["core"].get("/v1/todos/item-2", headers=TOK_INTAKE_HDR).json()
    assert t2["state"] == "parked" and t2["reason"] == "隔離が無い"


def test_sweep_parked_writes_task_state_due(ledger_env):
    env = ledger_env
    _create(env, {"id": "due-1", "title": "再確認", "state": "parked", "owner": "system", "reason": "外へ出した",
                  "check_at": timeutil.add_seconds(timeutil.now_ts(), -60)})
    before = len([c for c in env["transport"].calls if c.json and c.json.get("kind") == "task.state"])
    assert env["service"].sweep_parked() == 1
    due = [c for c in env["transport"].calls if c.json and c.json.get("kind") == "task.state"][before:]
    assert due and due[0].json["data"]["due"] is True and due[0].json["data"]["todo_id"] == "due-1"
    # 2回目の走査では同じ件に再び書かない（D-8 の走査で冪等）
    assert env["service"].sweep_parked() == 0


def test_waiting_user_to_todo_direct_transition_requires_answer(ledger_env):
    """回答と一体でのみ行う（S-9.3）。素の遷移は拒否。"""
    env = ledger_env
    _create(env, {"id": "t-ans1", "title": "質問", "state": "waiting_user", "owner": "user",
                  "card": {"question": "どちら？", "waiting_reason": "physical"}})
    r = _transition(env, "t-ans1", {"to": "todo"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "answer_required"

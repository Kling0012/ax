"""T-183 取り込み専用の書き込みの口（S-9.5、S-15.5、F-18.4）。

偽のコンテナを ax-work に置く代わりに、台帳の作業側の口（8081 相当）を TestClient で試す。
トークンは hibi-state 用と memory-digest 用の2つ（conftest の b3_tokens）。
"""
from __future__ import annotations

from datetime import timedelta

from axcommon import timeutil
from axcommon.defaults import Defaults

from core.ledger.service import TOKYO
from tests.b3.conftest import TOK_DIGEST, TOK_HIBI, TOK_HQ, TOK_INTAKE

HIBI = {"Authorization": "Bearer " + TOK_HIBI}
DIGEST = {"Authorization": "Bearer " + TOK_DIGEST}
HQ = {"Authorization": "Bearer " + TOK_HQ, "X-AX-Via": "hq"}


def _focus_body(as_of, state="focus", since=None):
    return {"state": state, "since": since or "2026-11-05T20:00:00+09:00", "as_of": as_of}


def _daily_body(date, as_of, segments=None):
    segs = segments if segments is not None else [
        {"start": f"{date}T09:00:00+09:00", "end": f"{date}T10:00:00+09:00", "state": "focus", "category": "work",
         "session_title": "実装"},
    ]
    return {"date": date, "tz": "Asia/Tokyo", "as_of": as_of, "segments": segs}


def _jst_date(offset_days=0):
    return (timeutil.now().astimezone(TOKYO) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _state_recorded(env):
    return [c.json for c in env["transport"].calls if c.json and c.json.get("kind") == "state.recorded"]


# ---- (a) 集中状態 ----


def test_t183_a_focus_state_put_idempotent_and_stale(ledger_env):
    env = ledger_env
    as_of = timeutil.now_ts()
    r = env["work"].put("/v1/focus-state", json=_focus_body(as_of), headers=HIBI)
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True and r.json()["kind"] == "focus" and r.json()["version"] == 1
    r2 = env["work"].put("/v1/focus-state", json=_focus_body(as_of), headers=HIBI)
    assert r2.status_code == 200 and r2.json()["stored"] is False and r2.json()["version"] == 1
    assert len(_state_recorded(env)) == 1  # 事象は1件だけ
    # 同じ as_of で内容が違えば 409
    r3 = env["work"].put("/v1/focus-state", json=_focus_body(as_of, state="away"), headers=HIBI)
    assert r3.status_code == 409
    # 古い as_of は 409（stale_as_of）
    r4 = env["work"].put("/v1/focus-state", json=_focus_body("2026-11-01T00:00:00.000Z"), headers=HIBI)
    assert r4.status_code == 409 and r4.json()["error"]["code"] == "stale_as_of"
    # 新しければ置き換わる（PUT は全置換）
    newer = timeutil.format_ts(timeutil.parse_ts(as_of) + timedelta(seconds=30))
    r5 = env["work"].put("/v1/focus-state", json=_focus_body(newer, state="break"), headers=HIBI)
    assert r5.status_code == 200 and r5.json()["stored"] is True and r5.json()["version"] == 2
    g = env["work"].get("/v1/focus-state", headers=HIBI)
    assert g.status_code == 200 and g.json()["state"] == "break" and g.json()["as_of"] == newer


# ---- (b) 日次状態の日付の窓 ----


def test_t183_b_daily_state_date_window_and_limits(ledger_env):
    env = ledger_env
    today, yesterday, older = _jst_date(0), _jst_date(-1), _jst_date(-2)
    as_of = timeutil.now_ts()
    r = env["work"].put(f"/v1/daily-state/{today}", json=_daily_body(today, as_of), headers=HIBI)
    assert r.status_code == 200 and r.json()["stored"] is True, r.text
    r = env["work"].put(f"/v1/daily-state/{yesterday}", json=_daily_body(yesterday, as_of), headers=HIBI)
    assert r.status_code == 200
    # 一昨日と未来の日付は 422（date_out_of_window）
    for date in (older, _jst_date(1)):
        rr = env["work"].put(f"/v1/daily-state/{date}", json=_daily_body(date, as_of), headers=HIBI)
        assert rr.status_code == 422 and rr.json()["error"]["code"] == "date_out_of_window", rr.text
    # パスと本文の date の不一致は 422
    rr = env["work"].put(f"/v1/daily-state/{today}", json=_daily_body(yesterday, as_of), headers=HIBI)
    assert rr.status_code == 422
    # segments が S-17 hibi_segments_max を1件超えると 422
    limit = int(Defaults.load().get("hibi_segments_max"))
    many = [{"start": f"{today}T09:00:00+09:00", "end": f"{today}T09:05:00+09:00", "state": "focus",
             "category": "work"} for _ in range(limit + 1)]
    rr = env["work"].put(f"/v1/daily-state/{today}", json=_daily_body(today, as_of, segments=many),
                         headers=HIBI)
    assert rr.status_code == 422, rr.text
    # 本文が hibi_body_max_kib を超えると 413（件数は上限内に保つ。検査の順は D-8: 件数 → サイズ）
    big = [{"start": f"{today}T09:00:00+09:00", "end": f"{today}T09:05:00+09:00", "state": "focus",
            "category": "work", "session_title": "あ" * 40} for _ in range(limit)]
    rr = env["work"].put(f"/v1/daily-state/{today}", json=_daily_body(today, as_of, segments=big), headers=HIBI)
    assert rr.status_code == 413, rr.text


# ---- (c) 値の検査 ----


def test_t183_c_unknown_extra_future_are_rejected(ledger_env):
    env = ledger_env
    # unknown は書けない
    r = env["work"].put("/v1/focus-state", json={"state": "unknown", "since": "2026-11-05T20:00:00+09:00",
                                                 "as_of": timeutil.now_ts()}, headers=HIBI)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_not_recorded"
    # 追加の項目は 422（schema）
    r = env["work"].put("/v1/focus-state", json={**_focus_body(timeutil.now_ts()), "window_title": "秘密の窓"},
                        headers=HIBI)
    assert r.status_code == 422 and r.json()["error"]["code"] == "schema"
    # 現在より hibi_future_skew_min 分より先の as_of は 422
    future = timeutil.format_ts(timeutil.now() + timedelta(minutes=30))
    r = env["work"].put("/v1/focus-state", json=_focus_body(future), headers=HIBI)
    assert r.status_code == 422, r.text


# ---- (d) 秘密検査 ----


def test_t183_d_secret_in_body_is_rejected_and_not_stored(ledger_env):
    env = ledger_env
    body = {**_focus_body(timeutil.now_ts()), "session_title": "FICTIONAL"}
    # schema は追加の項目を拒ぶので、秘密は既存の項目に入れる（session_title は日次の帯の中）
    today = _jst_date(0)
    daily = _daily_body(today, timeutil.now_ts(), segments=[
        {"start": f"{today}T09:00:00+09:00", "end": f"{today}T09:30:00+09:00", "state": "focus",
         "category": "work", "session_title": "FICTIONAL の秘密"}])
    r = env["work"].put(f"/v1/daily-state/{today}", json=daily, headers=HIBI)
    assert r.status_code == 422 and r.json()["error"]["code"] == "secret_detected", r.text
    # 保存されていない
    g = env["work"].get(f"/v1/daily-state/{today}", headers=HIBI).json()
    assert g["version"] == 0 and g["segments"] == []
    assert "FICTIONAL" not in str([c.body for c in env["transport"].calls])


def test_state_and_history_roll_back_together_on_storage_failure(ledger_env, monkeypatch):
    import sqlite3

    env = ledger_env
    as_of = timeutil.now_ts()
    original = _focus_body(as_of)
    assert env["work"].put("/v1/focus-state", json=original, headers=HIBI).status_code == 200
    events_before = len(_state_recorded(env))

    def fail_history(*args, **kwargs):
        raise sqlite3.OperationalError("injected storage failure")

    monkeypatch.setattr(env["service"].store, "add_state_history", fail_history)
    newer = timeutil.add_seconds(as_of, 1)
    response = env["work"].put("/v1/focus-state", json=_focus_body(newer, "away"), headers=HIBI)
    assert response.status_code == 503
    assert env["service"].store.get_focus() == (original, as_of, 1)
    assert len(env["service"].state_history("focus")) == 1
    assert len(_state_recorded(env)) == events_before


def test_state_rejects_invalid_calendar_timestamp(ledger_env):
    response = ledger_env["work"].put(
        "/v1/focus-state", json=_focus_body("2026-02-30T00:00:00+09:00"), headers=HIBI)
    assert response.status_code == 422
    assert ledger_env["service"].store.get_focus()[2] == 0


def test_state_history_requires_core_credential(ledger_env):
    assert ledger_env["core"].get("/v1/state-history/focus").status_code == 401
    assert ledger_env["core"].get("/v1/state-history/focus", headers=HIBI).status_code == 401
    assert ledger_env["core"].get("/v1/state-history/focus", headers=HQ).status_code == 200


def test_secret_scanner_unavailable_rejects_only_write(ledger_env, monkeypatch):
    import axcommon.redact
    from core.ledger.service import default_secret_scan

    def unavailable():
        raise OSError("injected scanner failure")

    monkeypatch.setattr(axcommon.redact, "from_environment", unavailable)
    ledger_env["service"].secret_scan = default_secret_scan
    response = ledger_env["work"].put("/v1/focus-state", json=_focus_body(timeutil.now_ts()), headers=HIBI)
    assert response.status_code == 503
    assert ledger_env["work"].get("/v1/focus-state", headers=HIBI).json()["version"] == 0
    assert not _state_recorded(ledger_env)


def test_state_concurrent_newer_and_older_writes_keep_newest(ledger_env):
    from concurrent.futures import ThreadPoolExecutor
    from core.ledger import Reject

    service = ledger_env["service"]
    now = timeutil.now_ts()
    newer = timeutil.add_seconds(now, 1)

    def write(as_of):
        try:
            return service.put_focus(_focus_body(as_of), issuer="plugin:hibi-state")
        except Reject as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(write, [newer, now]))
    assert service.get_focus()["as_of"] == newer
    assert service.state_history("focus")[-1]["after"]["as_of"] == newer


def test_daily_get_preserves_timezone(ledger_env):
    date = _jst_date()
    body = _daily_body(date, timeutil.now_ts())
    assert ledger_env["work"].put(f"/v1/daily-state/{date}", json=body, headers=HIBI).status_code == 200
    assert ledger_env["work"].get(f"/v1/daily-state/{date}", headers=HIBI).json()["tz"] == "Asia/Tokyo"


# ---- (e) ほかのプラグインと無効なトークン ----


def test_t183_e_other_plugins_and_bad_tokens_are_rejected(ledger_env):
    env = ledger_env
    as_of = timeutil.now_ts()
    # memory-digest のトークンでは2つの PUT と GET は 403
    for method, path in (("put", "/v1/focus-state"), ("get", "/v1/focus-state"),
                         ("put", f"/v1/daily-state/{_jst_date(0)}"), ("get", f"/v1/daily-state/{_jst_date(0)}")):
        if method == "put":
            r = env["work"].put(path, json=_focus_body(as_of), headers=DIGEST)
        else:
            r = env["work"].get(path, headers=DIGEST)
        assert r.status_code == 403 and r.json()["error"]["code"] == "plugin_not_allowed", f"{method} {path}: {r.text}"
    # トークン無し・不明は 401
    for hdr in (None, {"Authorization": "Bearer not-in-table"}):
        kw = {"headers": hdr} if hdr else {}
        r = env["work"].put("/v1/focus-state", json=_focus_body(as_of), **kw)
        assert r.status_code == 401, r.text
        r = env["work"].get("/v1/focus-state", **kw)
        assert r.status_code == 401


# ---- (f) 作業側の口に載っていない API は 404 ----


def test_t183_f_other_apis_are_404_on_the_work_mouth(ledger_env):
    env = ledger_env
    todo = env["core"].post("/v1/todos", json={"id": "t183", "title": "試験", "state": "todo", "owner": "system"},
                            headers={"Authorization": "Bearer " + TOK_INTAKE})
    assert todo.status_code == 201, todo.text
    r = env["work"].put("/v1/goals/g1", json={"body": "目標"}, headers=HIBI)
    assert r.status_code == 404
    r = env["work"].post("/v1/todos/t183/transition", json={"to": "running"}, headers=HIBI)
    assert r.status_code == 404
    r = env["work"].put("/v1/schedules/s1", json={"body": "予定"}, headers=HIBI)
    assert r.status_code == 404
    r = env["work"].post("/v1/todos/t183/answer", json={"answer": "はい"}, headers=HIBI)
    assert r.status_code == 404
    r = env["work"].post("/v1/commands", json={"target": "issue-automation", "op": "run_now", "args": {}, "reason": "無関係"},
                         headers=HIBI)
    assert r.status_code == 404


# ---- (g) 中核の口 ----


def test_t183_g_core_mouth_allows_hq_credential_for_state(ledger_env):
    env = ledger_env
    as_of = timeutil.now_ts()
    # 資格が無ければ拒否
    r = env["core"].put("/v1/focus-state", json=_focus_body(as_of))
    assert r.status_code == 401
    # hq の資格の PUT と GET は従来どおり通る
    r = env["core"].put("/v1/focus-state", json=_focus_body(as_of), headers=HQ)
    assert r.status_code == 200 and r.json()["stored"] is True, r.text
    g = env["core"].get("/v1/focus-state", headers=HQ)
    assert g.status_code == 200 and g.json()["state"] == "focus"
    # 作業側の資格（プラグインのトークン）では中核の口の書き込みは通らない
    r = env["core"].put("/v1/focus-state", json=_focus_body(as_of), headers=HIBI)
    assert r.status_code == 401


# ---- (h) 履歴と事象、発行者の固定 ----


def test_t183_h_only_changed_writes_record_events_with_fixed_issuer(ledger_env):
    env = ledger_env
    today = _jst_date(0)
    as_of = timeutil.now_ts()
    # 発行者を偽っても（本文に issuer を足しても）schema で 422、記録は plugin:hibi-state に固定
    r = env["work"].put("/v1/focus-state", json={**_focus_body(as_of), "issuer": "plugin:evil"}, headers=HIBI)
    assert r.status_code == 422
    r = env["work"].put("/v1/focus-state", json=_focus_body(as_of), headers=HIBI)
    assert r.status_code == 200
    # 変わらない書き込みは事象を書かない
    r = env["work"].put("/v1/focus-state", json=_focus_body(as_of), headers=HIBI)
    assert r.status_code == 200 and r.json()["stored"] is False
    recs = _state_recorded(env)
    assert len(recs) == 1
    assert recs[0]["data"]["issuer"] == "plugin:hibi-state" and recs[0]["data"]["changed"] is True
    # 変わった書き込みだけ増える
    newer = timeutil.format_ts(timeutil.parse_ts(as_of) + timedelta(seconds=10))
    r = env["work"].put("/v1/focus-state", json=_focus_body(newer, state="away"), headers=HIBI)
    assert r.status_code == 200 and r.json()["stored"] is True
    assert len(_state_recorded(env)) == 2
    # 履歴に変更前後が残る
    hist = env["core"].get("/v1/state-history/focus", headers=HQ).json()["history"]
    assert len(hist) == 2 and hist[0]["before"] is None and hist[1]["before"]["state"] == "focus"


# ---- 追加: 作業側の口の ToDo の作成と読み取り（F-18.4）----


def test_work_mouth_todo_creation_fixes_creator_to_plugin_id(ledger_env):
    env = ledger_env
    r = env["work"].post("/v1/todos", json={"id": "by-plugin", "title": "部品からの依頼", "state": "todo",
                                            "owner": "system", "created_by": "偽の作成者"}, headers=DIGEST)
    assert r.status_code == 201, r.text
    # 作成者はプラグイン ID に固定する（本文の値は信用しない）
    todo = env["work"].get("/v1/todos/by-plugin", headers=DIGEST).json()
    assert todo["created_by"] == "plugin:memory-digest"

"""T-117: 投影の形と再現（S-3.6、D-3）。Store の部分は starlette なしでも通る。HTTP の部分は starlette がある環境で通す。"""
import pytest

from core.timeline import projectors
from core.timeline.archive import TimelineArchive
from core.timeline.store import RebuildError, Store
from tests.fakes.b1.events import full_sequence, make_event


def row(proj, name, key):
    for r in proj["rows"]:
        if r["key"] == key:
            return r
    raise AssertionError(f"{name}/{key} が無い")


def loaded(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    events = full_sequence()
    for event in events:
        event["source"]["host"] = "hub-host"
        if event["kind"].startswith("plugin."):
            event["subject"] = "issue-automation"
        if event["kind"] == "run.state":
            event["subloop"] = "issue-automation"
        command = event["data"].get("command", {})
        if "target" in command:
            command["target"] = "issue-automation"
    s.append(events)
    return s


def test_names_are_exactly_the_five(tmp_path):
    s = loaded(tmp_path)
    assert projectors.PROJECTION_NAMES == ("plugins", "runs", "jobs", "controls", "health")
    for n in projectors.PROJECTION_NAMES:
        assert s.projection(n)["name"] == n
    with pytest.raises(KeyError):
        s.projection("todos")


def test_upto_seq_equals_max_seq_and_row_upto_seq_is_last_event(tmp_path):
    s = loaded(tmp_path)
    p = s.projection("runs")
    assert p["upto_seq"] == s.max_seq()
    ev = [e for e in s.list_events(limit=1000) if e["kind"] == "run.state" and e["subject"] == "RUN03"][0]
    assert row(p, "runs", "RUN03")["upto_seq"] == ev["seq"]


def test_plugins_rows(tmp_path):
    s = loaded(tmp_path)
    r = row(s.projection("plugins"), "plugins", "issue-automation")["value"]
    assert r == {"manifest_version": "1.0.0", "mode": "draining", "registered_at": "2026-11-05T12:00:01.000Z", "mode_changed_at": "2026-11-05T12:00:02.000Z"}


def test_plugin_mode_without_registration_creates_row_with_null_manifest(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("plugin.mode", "p9", {"mode": "pause"}, ts="2026-11-05T12:00:00.000Z")])
    v = row(s.projection("plugins"), "plugins", "p9")["value"]
    assert v == {"manifest_version": None, "mode": "pause", "registered_at": None, "mode_changed_at": "2026-11-05T12:00:00.000Z"}


def test_registered_again_overwrites_fields_and_keeps_mode_changed_at(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("plugin.registered", "p", {"manifest_version": "1", "mode": "run"}, ts="2026-11-05T12:00:00.000Z"),
              make_event("plugin.mode", "p", {"mode": "pause"}, ts="2026-11-05T12:01:00.000Z"),
              make_event("plugin.registered", "p", {"manifest_version": "2", "mode": "run"}, ts="2026-11-05T12:02:00.000Z")])
    v = row(s.projection("plugins"), "plugins", "p")["value"]
    assert v == {"manifest_version": "2", "mode": "run", "registered_at": "2026-11-05T12:02:00.000Z", "mode_changed_at": "2026-11-05T12:01:00.000Z"}


@pytest.mark.parametrize("state,active", [("planned", False), ("launching", True), ("submitted", True), ("running", True), ("completed", False),
                                          ("failed", False), ("timed_out", False), ("cancelled", False), ("lost", False), ("skipped", False),
                                          ("ejected", False), ("missed", False)])
def test_run_active_only_for_launching_submitted_running(tmp_path, state, active):
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("run.state", "R", {"state": state, "reason": "why"}, subloop="issue-automation", project="dreamforge")])
    v = row(s.projection("runs"), "runs", "R")["value"]
    assert v["active"] is active and v["state"] == state and v["subloop"] == "issue-automation" and v["project"] == "dreamforge" and v["reason"] == "why"


def test_run_layers_events_in_seq_order_last_wins(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("run.state", "R", {"state": "planned"}, ts="2026-11-05T12:00:00.000Z"),
              make_event("run.state", "R", {"state": "running"}, ts="2026-11-05T12:01:00.000Z"),
              make_event("run.state", "R", {"state": "failed", "reason": "boom"}, ts="2026-11-05T12:02:00.000Z")])
    v = row(s.projection("runs"), "runs", "R")["value"]
    assert v == {"subloop": "demo-loop", "project": "demo", "state": "failed", "reason": "boom", "first_ts": "2026-11-05T12:00:00.000Z",
                 "last_ts": "2026-11-05T12:02:00.000Z", "active": False}


@pytest.mark.parametrize("state,active", [("queued", False), ("launching", True), ("submitted", True), ("running", True), ("stalled", True), ("completed", False)])
def test_job_active_includes_stalled_but_not_queued(tmp_path, state, active):
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("job.state", "pool", {"job_id": "J1", "state": state, "model": "m", "role": "impl"})])
    v = row(s.projection("jobs"), "jobs", "J1")["value"]
    assert v["active"] is active and v["model"] == "m" and v["role"] == "impl"


def test_job_keeps_model_and_role_when_later_event_omits_them(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("job.state", "pool", {"job_id": "J1", "state": "running", "model": "m", "role": "impl"}),
              make_event("job.state", "pool", {"job_id": "J1", "state": "completed"})])
    v = row(s.projection("jobs"), "jobs", "J1")["value"]
    assert v["state"] == "completed" and v["model"] == "m" and v["role"] == "impl"


def test_controls_lifecycle(tmp_path):
    s = loaded(tmp_path)
    p = s.projection("controls")
    c1 = row(p, "controls", "CMD1")["value"]
    assert c1["state"] == "expired_pending" and c1["active"] is True                    # 期限後も効力が残る（S-5.6）
    assert c1["expires_at"] == "2026-11-05T14:00:00.000Z"                               # renewed で更新された
    assert c1["applied_at"] is not None and c1["target"] == "issue-automation" and c1["op"] == "pause" and c1["on_expiry"] == "resume"
    c2 = row(p, "controls", "CMD2")["value"]
    assert c2["state"] == "released" and c2["active"] is False and c2["applied_at"] is None
    assert {r["key"] for r in p["rows"]} == {"CMD1", "CMD2"}                              # control.rejected は行を作らない


def test_renewed_returns_state_to_active(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    seq = full_sequence()
    issued = [e for e in seq if e["kind"] == "control.issued"][0]
    expired = [e for e in seq if e["kind"] == "control.expired_pending"][0]
    renewed = [e for e in seq if e["kind"] == "control.renewed"][0]
    s.append([issued, expired, renewed])
    assert row(s.projection("controls"), "controls", "CMD1")["value"]["state"] == "active"


def test_health_row(tmp_path):
    s = loaded(tmp_path)
    p = s.projection("health")
    assert row(p, "health", "ledger")["value"] == {"host": "hub-host", "last_beat_at": "2026-11-05T12:00:" + f"{len(full_sequence()):02d}" + ".000Z",
                                                   "uptime_s": 20, "lag_s": 1}


def test_active_filter_and_key_filter_and_limit(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    evs = [make_event("run.state", f"R{i:03d}", {"state": "running" if i % 2 == 0 else "completed"}) for i in range(60)]
    s.append(evs)
    act = s.projection("runs", active=True, limit=5)                    # active=true は limit を無視してすべて返す
    assert len(act["rows"]) == 30 and all(r["value"]["active"] for r in act["rows"])
    assert [r["key"] for r in s.projection("runs", key="R004")["rows"]] == ["R004"]
    lim = s.projection("runs", limit=7)["rows"]
    assert len(lim) == 7 and lim[0]["upto_seq"] > lim[-1]["upto_seq"]    # 新しい順


def test_missing_required_fields_skip_only_the_projection_but_append_succeeds(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    bad = [make_event("run.state", "R1", {}),                                               # data.state が無い
           make_event("job.state", "pool", {"state": "running"}),                           # data.job_id が無い
           make_event("job.state", "pool", {"job_id": "J1"}),                               # data.state が無い
           make_event("control.issued", "C", {}),                                           # data.command が無い
           make_event("control.applied", "C", {"command": {}}),                             # command.id が無い
           make_event("plugin.registered", "p", {"manifest_version": "1"}),                 # data.mode が無い
           make_event("plugin.mode", "p", {})]
    res = s.append(bad)
    assert len(res) == 7 and not any(r["duplicate"] for r in res)                           # 追記は成功
    assert s.projection_skipped() == 7
    for n in projectors.PROJECTION_NAMES:
        assert s.projection(n)["rows"] == []
    # 未知の kind と表にない kind は影響しない（skipped も増えない）
    s.append([make_event("task.state", "t", {}), make_event("totally.unknown", "u", {"x": 1}), make_event("control.rejected", "C9", {})])
    assert s.projection_skipped() == 7 and s.max_seq() == 10


def test_projection_changes_only_through_events_not_the_clock(tmp_path, fake_clock):
    s = loaded(tmp_path)
    before = s.projection_snapshot()
    fake_clock.advance(86400 * 40)
    s.rebuild_projections()
    assert s.projection_snapshot() == before


def test_rebuild_equals_before_for_every_row(tmp_path):
    s = loaded(tmp_path)
    s.append([make_event("run.state", "X", {})])                                         # skip を 1 つ混ぜる
    before, skipped = s.projection_snapshot(), s.projection_skipped()
    res = s.rebuild_projections()
    assert s.projection_snapshot() == before and s.projection_skipped() == skipped
    assert res["rows"] == len(before) and res["archived_events"] == 0


def test_rebuild_clears_a_corrupted_projection(tmp_path):
    s = loaded(tmp_path)
    before = s.projection_snapshot()
    s._conn.execute("DELETE FROM projections WHERE name='runs'")
    s._conn.execute("UPDATE projections SET value='{}' WHERE name='plugins'")
    assert s.projection_snapshot() != before
    s.rebuild_projections()
    assert s.projection_snapshot() == before


def test_rebuild_includes_archived_range_even_when_db_lost_it(tmp_path):
    """退避済みの区間（archive の JSON Lines）を先に読み、DB に無い seq も含めて同じ表を作る。"""
    full = full_sequence()
    src = Store(tmp_path / "src.sqlite")
    src.append(full)
    want = src.projection_snapshot()
    arch = TimelineArchive(tmp_path / "archive")
    res = arch.export(src, to_seq=12)
    assert res["exported"] == 12 and res["last_seq"] == 12
    # DB は後半だけ（前半は退避済みで DB から無くなった状態を模す）。seq は元のまま
    part = Store(tmp_path / "part.sqlite")
    part._conn.execute("DROP TRIGGER events_no_update")
    for e in src.list_events(after=12, limit=1000):
        import json
        part._conn.execute("INSERT INTO events(seq, id, ts, kind, subject, project, subloop, component, doc) VALUES (?,?,?,?,?,?,?,?,?)",
                           (e["seq"], e["id"], e["ts"], e["kind"], e["subject"], e["project"], e["subloop"], e["source"]["component"],
                            json.dumps({k: v for k, v in e.items() if k != "seq"}, ensure_ascii=False)))
    only_db = part.rebuild_projections()
    assert part.projection_snapshot() != want                                              # 退避済みを読まないと足りない
    r = part.rebuild_projections(arch.iter_events())
    assert r["archived_events"] == 12
    assert part.projection_snapshot() == want                                              # 全行で value と upto_seq が一致
    assert only_db["archived_events"] == 0


def test_rebuild_with_overlap_between_archive_and_db_does_not_double_apply(tmp_path):
    s = loaded(tmp_path)
    before = s.projection_snapshot()
    arch = TimelineArchive(tmp_path / "archive")
    arch.export(s, to_seq=10)                                                              # DB にも残っている
    s.rebuild_projections(arch.iter_events())
    assert s.projection_snapshot() == before


def test_rebuild_refuses_a_corrupted_archive_and_keeps_the_old_table(tmp_path):
    s = loaded(tmp_path)
    before = s.projection_snapshot()
    arch = TimelineArchive(tmp_path / "archive")
    arch.export(s, to_seq=8)
    f = next((tmp_path / "archive" / "timeline").glob("*.jsonl"))
    f.write_text(f.read_text(encoding="utf-8") + "\n", encoding="utf-8")                   # 内容が索引の SHA-256 と違う
    with pytest.raises(RebuildError):
        s.rebuild_projections(arch.iter_events())
    assert s.projection_snapshot() == before


def test_admin_cli_rebuild_and_verify(tmp_path, capsys):
    from core.timeline import admin
    s = loaded(tmp_path)
    s.close()
    code = admin.main(["rebuild-projections", "--db", str(tmp_path / "t.sqlite"), "--archive", str(tmp_path / "archive"), "--verify"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"verified": true' in out


# --- 書き出し（archive/timeline/）-----------------------------------------------------------------
def _days(store, days):
    evs = []
    for d in days:
        for h in (1, 2):
            evs.append(make_event("run.state", f"R-{d}-{h}", {"state": "running"}, ts=f"2026-10-{d:02d}T{h:02d}:00:00.000Z"))
    store.append(evs)


def test_export_splits_by_day_keeps_a_contiguous_range_and_records_an_index(tmp_path):
    import hashlib
    import json
    from tests.b1.helpers import ext_errors
    s = Store(tmp_path / "t.sqlite")
    _days(s, [1, 2, 3])
    arch = TimelineArchive(tmp_path / "a")
    res = arch.export(s, before_ts="2026-10-03T00:00:00.000Z")                       # 3 日の分は含めない（排他）
    assert res["exported"] == 4 and res["last_seq"] == 4 and len(res["files"]) == 2
    idx = arch.load_index()
    assert ext_errors("timeline-archive-index", idx) == []
    assert [(f["date"], f["first_seq"], f["last_seq"], f["count"]) for f in idx["files"]] == [("2026-10-01", 1, 2, 2), ("2026-10-02", 3, 4, 2)]
    for f in idx["files"]:
        body = (tmp_path / "a" / "timeline" / f["file"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == f["sha256"] and f["bytes"] == len(body)
        assert all("seq" in json.loads(line) for line in body.decode().splitlines())
    again = arch.export(s, before_ts="2026-10-03T00:00:00.000Z")                     # 続きから: 何も書かない
    assert again["exported"] == 0 and again["files"] == []
    more = arch.export(s, to_seq=5)                                                    # 続き（seq 5 まで）
    assert more["exported"] == 1 and arch.last_seq() == 5
    assert [e["seq"] for e in arch.iter_events()] == [1, 2, 3, 4, 5]
    assert [e["seq"] for e in arch.iter_events(after=3)] == [4, 5]


def test_export_stops_at_the_first_event_that_does_not_match(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.append([make_event("run.state", "A", {"state": "running"}, ts="2026-10-01T01:00:00.000Z"),
              make_event("run.state", "B", {"state": "running"}, ts="2026-11-30T01:00:00.000Z"),      # 新しい
              make_event("run.state", "C", {"state": "running"}, ts="2026-10-01T02:00:00.000Z")])     # 遅れて届いた古い事象
    arch = TimelineArchive(tmp_path / "a")
    assert arch.export(s, before_ts="2026-11-01T00:00:00.000Z")["exported"] == 1                      # 範囲は seq の 1 から連続
    assert arch.last_seq() == 1                                                                         # C は B の後ろなので書き出さない（連続を守る）


def test_missing_exported_file_is_skipped_so_the_db_fills_in(tmp_path):
    s = loaded(tmp_path)
    before = s.projection_snapshot()
    arch = TimelineArchive(tmp_path / "a")
    arch.export(s, to_seq=6)
    for f in (tmp_path / "a" / "timeline").glob("*.jsonl"):
        f.rename(tmp_path / "gone.jsonl")                                          # 退避先へ移って手元に無い
    assert arch.missing_files() and list(arch.iter_events()) == []
    s.rebuild_projections(arch.iter_events())
    assert s.projection_snapshot() == before


def test_export_never_modifies_the_db(tmp_path):
    s = loaded(tmp_path)
    before = s.list_events(limit=1000)
    TimelineArchive(tmp_path / "a").export(s, to_seq=s.max_seq())
    assert s.list_events(limit=1000) == before

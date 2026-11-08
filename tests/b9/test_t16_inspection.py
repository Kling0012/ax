"""T-16 点検（S-13、D-12）。本物の control・timeline・ledger に、点検係と B7 の実行系（プラグイン）をつなぐ。

- 減らし込み中に新規 run・item は開始しない。既存 item の後続ノードと長い作業は止まらない。
- 管理役が黙ると自動で再開する（点検の中だけ）。点検の外では、黙っても元に戻らない。
"""
import asyncio
from datetime import timedelta

from axcommon import timeutil
from tests.b9.helpers import in_min, noop, reply, seq_of
from tests.fakes.model_gateway import Reply


def run(coro):
    return asyncio.run(coro)


def _to_notice(w, insp):
    w.clock.advance((timeutil.parse_ts(insp.state["next_at"]) - w.clock.now()).total_seconds() - insp.cfg.notice_min * 60 + 1)


def _to_start(w, insp):
    w.clock.advance((timeutil.parse_ts(insp.state["next_at"]) - w.clock.now()).total_seconds() + 1)


def phases(w):
    return [e["data"]["phase"] for e in w.events("inspection.phase")]


def test_schedule_notice_then_draining_commands_for_every_plugin(world):
    insp = world.inspector()
    assert phases(world) == [] and insp.state["phase"] == "idle"
    assert insp.run_once() == []                                          # まだ予告の時刻でない
    _to_notice(world, insp)
    assert insp.run_once() == ["notice"]
    n = world.events("inspection.phase")[0]["data"]
    assert n["phase"] == "notice" and n["plan"]["interval_h"] == 24 and n["plan"]["inspection_id"]
    _to_start(world, insp)
    did = insp.run_once()
    assert did[:1] == ["draining"]
    cmds = world.control.list_commands()
    assert sorted(c["target"] for c in cmds) == ["pa", "pb", "pc", "worker-pool"]               # 全プラグインに 1 つずつ（資源プラグインも）
    for c in cmds:
        assert c["issued_by"] == "inspection" and c["op"] == "set_mode" and c["args"] == {"mode": "draining"}
        assert c["on_expiry"] == "release"                                                       # 点検係が落ちても必ず解除される
        assert timeutil.parse_ts(c["expires_at"]) - world.clock.now() <= timedelta(hours=insp.cfg.drain_max_h)
    assert phases(world)[:3] == ["notice", "draining", "wait"]
    # 動いている物が無ければ、そのまま見直しの依頼（段 4〜7 を管理役に頼む）まで進む
    assert "review_request" in did and insp.state["phase"] == "review_request"


def test_draining_starts_no_new_run_or_item_but_existing_items_run_to_the_end(world):
    ex = world.add_plugin_runtime("pa", items=2, sleep_s=0.05, mpi=1)
    insp = world.inspector()

    async def go():
        await ex.run_once()
        await asyncio.sleep(0.01)                     # A は s1 の途中
        _to_notice(world, insp)
        insp.run_once()
        _to_start(world, insp)
        insp.tick()                                    # 点検係が draining を出す → control が /apply で pa に届ける
        mode_mid = ex.mode
        await ex.drain()
        rid = await ex.run_once()                      # 新しい run は始めない
        await ex.tick()
        await ex.drain()
        return mode_mid, rid

    from tests.fakes.b7 import control as b7control

    mode_mid, rid = run(go())
    assert mode_mid == "draining"
    states = {i["item_id"]: i["state"] for i in ex.store.items() if not i["item_id"].startswith("run:")}
    assert states == {"pa-A": "completed", "pa-B": "planned"}                       # 既存 item は最後まで、新規 item は始めない
    assert ("pa-A", "s3") in b7control.COUNTS and ("pa-B", "s1") not in b7control.COUNTS
    assert rid is None and ex.store.runs()[-1]["state"] == "skipped" and ex.store.runs()[-1]["reason"] == "mode:draining"
    # 点検係は cancel を出さない（長い作業は止めない）
    assert {c["op"] for c in world.control.list_commands()} == {"set_mode"}


def test_long_work_is_not_stopped_and_the_inspection_proceeds_after_drain_wait(world):
    ex = world.add_plugin_runtime("pa", items=1, sleep_s=3600, mpi=1)
    insp = world.inspector()

    async def start():
        await ex.run_once()
        await asyncio.sleep(0.01)

    run(start())                                                  # pa-A が長い作業の最中（run.state running が時系列記録に載っている）
    assert any(r for r in world.timeline.store.projection("runs", active=True)["rows"])
    _to_notice(world, insp)
    insp.run_once()
    _to_start(world, insp)
    did = insp.tick()
    assert did == ["draining"] and insp.state["phase"] == "wait"
    world.clock.advance(60 * 60)                                  # drain_wait_h の途中。まだ動いているので待つ
    assert insp.tick() == [] and insp.state["phase"] == "wait"
    world.clock.advance(insp.cfg.drain_wait_h * 3600)
    assert insp.tick() == ["review_request"]                      # 達したら見直しへ。長い作業はそのまま
    assert {i["item_id"]: i["state"] for i in ex.store.items() if not i["item_id"].startswith("run:")}["pa-A"] in ("running", "planned")
    assert [c["op"] for c in world.control.list_commands()].count("cancel") == 0


def test_manager_silence_in_drain_resumes_automatically(world):
    ex = world.add_plugin_runtime("pa", items=1, sleep_s=3600, mpi=1)
    insp = world.inspector()

    async def start():
        await ex.run_once()
        await asyncio.sleep(0.01)

    run(start())                                   # 長い作業の最中なので、減らし込みの待ち（段 3）の間に管理役が黙る
    _to_notice(world, insp)
    insp.run_once()
    _to_start(world, insp)
    insp.tick()
    assert insp.state["phase"] == "wait"
    # 管理役が黙った → オブザーバーが点検係に「再開」を指示（S-11.2、S-13.3）
    world.emit("health.alert", "drain_silent:inspection", {"check": "drain_silent", "target": "inspection", "severity": "warn",
                                                           "destination": "inspection", "detail": "管理役が動かない"}, component="observer")
    did = insp.run_once()
    assert "resume" in did and insp.state["phase"] == "idle"
    assert all(c["state"] == "released" for c in world.control.list_commands())                  # 点検係の命令がすべて解除された
    world.poll_effective("pa")
    assert ex.mode == "run"                                                                       # プラグインは元の動作に戻る
    ph = phases(world)
    assert "review_request" not in ph and ph[-2:] == ["resume", "schedule"]                       # 段 4〜7 は飛ばし、段 8 を行う
    res = insp.state["last_result"]
    assert res["resume_reason"] == "silent" and res["quiet"] is False
    assert insp.state["interval_h"] == insp.cfg.initial_h                                        # 崩れが出たので初期値に戻す


def test_silence_outside_an_inspection_never_reverts_anything(world):
    """点検の外では、黙っても元に戻らない（R-O6、S-5.6）。管理役の期限切れの命令は expired_pending のまま効力を残す。"""
    world.add_plugin_runtime("pa")
    cmd = world.command("manager", target="pa", op="set_mode", args={"mode": "collect_only"}, reason="負荷", expires_at=in_min(10), on_expiry="ask_issuer")
    insp = world.inspector()
    world.clock.advance(10 * 3600)
    world.control.sweep_expiry()
    assert world.control.store.get_command(cmd["id"])["state"] == "expired_pending"
    assert world.control.effective("pa")["mode"] == "collect_only"                 # 効力は残る
    # 点検の外で drain_silent 警報が来ても、点検係は何も解除しない
    world.emit("health.alert", "drain_silent:inspection", {"check": "drain_silent", "target": "inspection", "severity": "warn",
                                                           "destination": "inspection"}, component="observer")
    insp.poll()
    assert insp.state["skip_review"] is False and insp.state["phase"] == "idle"
    world.clock.advance(5 * 24 * 3600)
    world.control.sweep_expiry()
    c = world.control.store.get_command(cmd["id"])
    assert c["state"] == "expired_pending" and world.control.effective("pa")["mode"] == "collect_only"
    # 点検が自分の命令だけを解除する: 管理役の命令には触れない
    _to_notice(world, insp)
    insp.tick()
    _to_start(world, insp)
    insp.tick()
    world.emit("health.alert", "d", {"check": "drain_silent", "destination": "inspection"}, component="observer")
    insp.run_once()
    assert world.control.store.get_command(cmd["id"])["state"] == "expired_pending"


def test_drain_commands_expire_by_themselves_if_the_inspector_dies(world):
    ex = world.add_plugin_runtime("pa")
    insp = world.inspector()
    _to_notice(world, insp)
    insp.run_once()
    _to_start(world, insp)
    insp.tick()
    assert world.control.effective("pa")["mode"] == "draining"
    world.clock.advance(insp.cfg.drain_max_h * 3600 + 5)          # 点検係はもう動かない
    world.control.sweep_expiry()
    assert all(c["state"] == "released" for c in world.control.list_commands())
    assert world.control.effective("pa")["mode"] == "run"
    world.poll_effective("pa")
    assert ex.mode == "run"


def test_a_plugin_that_does_not_answer_apply_does_not_block_the_others_or_the_inspector(world):
    world.add_plugin_runtime("pa")
    world.add_plugin_runtime("pb")
    world.net.stop("sl-pa")                                       # pa の /apply が届かない
    insp = world.inspector()
    _to_notice(world, insp)
    insp.run_once()
    _to_start(world, insp)
    did = insp.tick()
    assert did[0] == "draining" and len(world.control.list_commands()) == 4
    assert world.runtimes["pb"]["ex"].mode == "draining"          # 届いた先は draining になる
    world.poll_effective("pa")                                    # 届かなかった pa も effective を読めば合う（S-4.4）
    assert world.runtimes["pa"]["ex"].mode == "draining"


def _finish_review(world, insp, repairs=0, backups=0):
    for _ in range(backups):
        world.emit("backup.activated", "manager_trouble:silent", {"reason": "x"}, component="observer")
    world.emit("manager.decision", "manager", {"step": "review", "inspection_id": insp.state["inspection_id"], "repairs": repairs,
                                               "backup_activations": backups, "steps": {}}, component="manager")
    return insp.run_once()


def _run_inspection(world, insp, repairs=0, backups=0):
    _to_notice(world, insp)
    insp.run_once()
    _to_start(world, insp)
    insp.run_once()
    assert insp.state["phase"] == "review_request"
    return _finish_review(world, insp, repairs, backups)


def test_interval_stretches_after_quiet_inspections_and_resets_on_a_breakage(world):
    insp = world.inspector()
    seen = []
    for _ in range(3):
        _run_inspection(world, insp)
        seen.append(insp.state["interval_h"])
    assert seen == [24, 24, 36]                                   # 3 回静かで 1.5 倍（quiet_count=3、stretch=1.5）
    assert insp.state["quiet_streak"] == 3
    _run_inspection(world, insp)
    assert insp.state["interval_h"] == 54
    for _ in range(6):
        _run_inspection(world, insp)
    assert insp.state["interval_h"] == 168                         # 上限まで
    _run_inspection(world, insp, repairs=1)                       # 恒久修繕が出た = 崩れ
    assert insp.state["interval_h"] == 24 and insp.state["quiet_streak"] == 0
    _run_inspection(world, insp, backups=1)                       # 予備役の起動も崩れ
    assert insp.state["interval_h"] == 24
    sch = [e["data"]["plan"] for e in world.events("inspection.phase") if e["data"]["phase"] == "schedule"]
    assert sch[2]["interval_h"] == 36 and sch[-1]["quiet"] is False and sch[-1]["previous_interval_h"] == 24


def test_range_in_config_is_enforced(world, tmp_path):
    import pytest
    from core.inspector.config import InspectionConfig, InspectionConfigError

    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\ninterval_h: {initial: 24, min: 48, max: 24}\n", encoding="utf-8")
    with pytest.raises(InspectionConfigError):
        InspectionConfig.load(bad)
    out = tmp_path / "out.yaml"
    out.write_text("schema_version: 1\ninterval_h: {initial: 12, min: 24, max: 168}\n", encoding="utf-8")
    cfg = InspectionConfig.load(out)
    assert cfg.initial_h == 24 and cfg.clamped is True              # 範囲の外の初期値は範囲へ寄せ、寄せたことを残す
    insp = world.inspector(max_h=48)
    insp.state["interval_h"] = 40
    _run_inspection(world, insp)
    assert insp.state["interval_h"] <= 48


def test_state_survives_a_restart_and_a_second_instance_does_not_double_issue(world):
    insp = world.inspector()
    _to_notice(world, insp)
    insp.run_once()
    _to_start(world, insp)
    insp.tick()
    n = len(world.control.list_commands())
    again = world.inspector()                                     # 再起動: 状態ファイルから戻る
    assert again.state["phase"] == "review_request" and again.state["inspection_id"] == insp.state["inspection_id"]
    again.tick()
    assert len(world.control.list_commands()) == n


def test_review_timeout_goes_to_resume_and_is_not_quiet(world):
    insp = world.inspector()
    _to_notice(world, insp)
    insp.run_once()
    _to_start(world, insp)
    insp.run_once()
    assert insp.state["phase"] == "review_request"
    world.clock.advance(insp.cfg.drain_max_h * 3600 + 1)                       # 管理役が返さないまま上限
    insp.tick()
    assert insp.state["phase"] == "idle" and insp.state["last_result"]["resume_reason"] == "review_timeout"
    assert insp.state["last_result"]["quiet"] is False


def test_user_changes_are_recorded_in_the_review_phase_only(world):
    insp = world.inspector()
    insp.record_user_change([{"key": "role_candidates.impl", "before": ["free"], "after": ["flash"], "reason": "品質のため"}])
    assert phases(world) == []                                                  # 点検の外では review を書かない（減らし込みと取り違えない）
    _run_inspection(world, insp)
    rev = [e["data"]["plan"] for e in world.events("inspection.phase") if e["data"]["phase"] == "review"]
    assert rev[0]["changes"] == [{"key": "role_candidates.impl", "before": ["free"], "after": ["flash"], "reason": "品質のため"}]
    assert insp.state["changes"] == []


def test_phase_names_agree_with_the_observer(world):
    from core.observer.checks import DRAIN_PHASES
    from core.inspector import PHASE_DRAINING, PHASE_NOTICE, PHASE_RESUME, PHASE_REVIEW, PHASE_REVIEW_REQUEST, PHASE_SCHEDULE, PHASE_WAIT

    assert {PHASE_DRAINING, PHASE_WAIT, PHASE_REVIEW_REQUEST, PHASE_REVIEW} <= set(DRAIN_PHASES)
    assert not ({PHASE_NOTICE, PHASE_RESUME, PHASE_SCHEDULE} & set(DRAIN_PHASES))


def test_observer_detects_manager_silence_from_our_events_and_alerts_the_inspector(world):
    """オブザーバー（B8）の drain_silent 検査に、点検係の phase の事象と管理役の manager.cycle を食わせて、警報の宛先が inspection になる。"""
    from core.observer.checks import Ctx, Memory, check_drain_silent
    from core.observer.settings import Settings

    insp = world.inspector()
    m = world.manager()
    _to_notice(world, insp)
    insp.run_once()
    _to_start(world, insp)
    insp.tick()
    evs = world.timeline.store.list_events(after=0, kinds=["inspection.phase", "manager.cycle", "manager.decision", "error.classified"], limit=1000)
    mem = Memory(started=world.clock.now())
    s = Settings(host="ci", drain_silent_min=10)
    ctx = Ctx(settings=s, sources=None, now=world.clock.now() + timedelta(minutes=5), memory=(mem.ingest(evs, 3600, world.clock.now()) or mem), health=None, registered={})
    assert check_drain_silent(ctx) == []                                         # 5 分ではまだ
    ctx.now = world.clock.now() + timedelta(minutes=11)
    alerts = check_drain_silent(ctx)
    assert alerts and alerts[0].destinations == ("inspection",)
    m.run_cycle("tick")                                                          # 管理役が動けば（manager.cycle）、警報は出ない
    evs = world.timeline.store.list_events(after=0, kinds=["inspection.phase", "manager.cycle", "manager.decision"], limit=1000)
    mem2 = Memory(started=world.clock.now())
    mem2.ingest(evs, 3600, world.clock.now())
    ctx2 = Ctx(settings=s, sources=None, now=world.clock.now() + timedelta(minutes=9), memory=mem2, health=None, registered={})
    assert check_drain_silent(ctx2) == []


def test_manager_review_reply_completes_the_same_inspection(world):
    insp = world.inspector()
    mgr = world.manager()
    _to_notice(world, insp)
    insp.run_once()
    _to_start(world, insp)
    insp.run_once()
    inspection_id = insp.state["inspection_id"]
    # 本物の管理役が時系列記録から依頼を読み、偽の pool へ判断を頼む。
    world.pool.gw.script(*[reply(noop()) for _ in range(8)])
    mgr.fetch()
    mgr.run_cycle("tick")
    decisions = [e["data"] for e in world.events("manager.decision") if e["data"].get("step") == "review"]
    assert decisions and decisions[-1]["inspection_id"] == inspection_id
    assert set(decisions[-1]["steps"]) == {"4", "5", "6", "7"}
    assert world.pool.bodies and all(b["role"] == "manager" for b in world.pool.bodies)
    assert world.memory.remembered
    assert "resume" in insp.run_once()
    assert insp.state["last_result"]["resume_reason"] == "done"
    assert insp.state["last_result"]["inspection_id"] == inspection_id
    assert all(c["state"] == "released" for c in world.control.list_commands())


def test_review_reply_for_another_inspection_is_ignored(world):
    insp = world.inspector()
    _to_start(world, insp)
    insp.run_once()
    world.emit("manager.decision", "manager", {"step": "review", "inspection_id": "old-inspection", "repairs": 0}, component="manager")
    assert insp.run_once() == []
    assert insp.state["phase"] == "review_request" and insp.state["review_done"] is False


def test_user_changes_without_a_reason_are_rejected_atomically(world):
    import pytest

    insp = world.inspector()
    good = {"key": "role_candidates.impl", "before": ["free"], "after": ["flash"], "reason": "品質"}
    for reason in (None, "", "  ", 1):
        bad = {"key": "role_candidates.impl", "before": ["free"], "after": ["flash"]}
        if reason is not None:
            bad["reason"] = reason
        with pytest.raises(ValueError):
            insp.record_user_change([good, bad])
        assert insp.state["changes"] == []


def test_invalid_first_at_is_rejected_before_the_loop_starts(tmp_path):
    import pytest
    from core.inspector.config import InspectionConfig, InspectionConfigError

    path = tmp_path / "inspection.yaml"
    path.write_text('schema_version: 1\ninterval_h: {initial: 24, min: 24, max: 168}\nfirst_at: "2026-99-99T00:00:00Z"\n', encoding="utf-8")
    with pytest.raises(InspectionConfigError):
        InspectionConfig.load(path)


def test_restored_interval_respects_a_new_user_range(world):
    insp = world.inspector()
    insp.state["interval_h"] = 168
    insp.store.save(insp.state)
    again = world.inspector(max_h=48)
    assert again.state["interval_h"] == 48
    assert again.store.load()["interval_h"] == 48
    schedule = world.events("inspection.phase")[-1]["data"]
    assert schedule["phase"] == "schedule" and schedule["plan"]["previous_interval_h"] == 168


def test_timeline_outage_buffers_inspection_events_and_replays_them(world):
    insp = world.inspector()
    world.net.stop("timeline", caller="inspector")
    _to_start(world, insp)
    assert insp.run_once() == ["notice", "draining"]
    assert insp.state["phase"] == "wait" and insp.view()["timeline_ok"] is False
    assert not world.events("inspection.phase")
    world.net.start("timeline", caller="inspector")
    insp.sink.flush()
    assert phases(world) == ["notice", "draining", "wait"]
    assert insp.run_once() == ["review_request"]


def test_inspection_schemas_and_saved_state_are_valid(world, repo_root):
    import json
    from jsonschema import Draft202012Validator

    for name in ("inspection-config", "inspection-state"):
        schema = json.loads((repo_root / "schemas" / "ext" / "b9" / f"{name}.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        if name == "inspection-state":
            insp = world.inspector()
            Draft202012Validator(schema).validate(insp.store.load())


def test_undefined_user_change_contract_does_not_accept_writes(world):
    from core.inspector.app import create_inspector_app
    from starlette.testclient import TestClient

    insp = world.inspector()
    client = TestClient(create_inspector_app(insp, run_loop=False))
    response = client.post("/v1/review-changes", json={"changes": [{"key": "role_candidates.impl", "before": [], "after": ["flash"], "reason": "品質"}]})
    assert response.status_code == 501
    assert insp.state["changes"] == []
    assert client.get("/v1/inspection").status_code == 200


def test_t14_priority_from_standby_to_inspection(world):
    from axcommon.events import EventSink
    from core.observer.model import Alert
    from core.observer.observer import Observer
    from core.observer.settings import Settings
    from core.observer.standby.backup import Standby
    from tests.fakes.b10a.fake_manager import FakeManager

    clients = world.clients("manager")
    sink = EventSink(clients.timeline, "observer", "ci", world.tmp / "backup-events")
    standby = Standby(runner=FakeManager(), ledger=clients.ledger, timeline=clients.timeline,
                      sink=sink, data_dir=world.tmp / "standby", inline=True)
    observer = Observer(Settings(state_dir=str(world.tmp), data_dir=str(world.tmp / "observer")), clients.timeline, sink)
    observer.set_hook("backup", standby.on_alert)
    alert = Alert("manager_trouble", "silent", ("backup",), "high", "manager silent")
    observer.deliver(alert)
    observer.deliver(alert)
    todos_before = clients.ledger.todos()
    priority = [t for t in todos_before if t.get("reason") == "backup:root_cause:top"]
    assert len(priority) == 1
    inspector = world.inspector()
    manager = world.manager()
    _to_notice(world, inspector)
    inspector.run_once()
    _to_start(world, inspector)
    inspector.run_once()
    world.pool.gw.script(*[reply(noop()) for _ in range(8)])
    manager.fetch()
    manager.run_cycle("tick")
    decisions = [e["data"] for e in world.events("manager.decision") if e["data"].get("step") == "review"]
    assert decisions[-1]["report"]["priority_repairs"][0]["id"] == priority[0]["id"]
    assert any(priority[0]["id"] in body["prompt"] for body in world.pool.bodies)
    todos_after = clients.ledger.todos()
    assert not [t for t in todos_after if t["title"].startswith("【予備役の件】")]
    assert {t["id"] for t in todos_before} <= {t["id"] for t in todos_after}
    assert "resume" in inspector.run_once()

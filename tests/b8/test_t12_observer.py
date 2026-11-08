"""T-12 オブザーバー（S-11.1、S-11.2、D-10.1）: 偽の時系列記録と偽の時計で、検査と警報の重複除去を確かめる。"""
import json

import pytest

from core.observer.checks import DEFAULT_CHECKS
from core.observer.model import Alert
from tests.b8.helpers import Rig


@pytest.fixture
def rig(tmp_path, fake_clock, fake_transport):
    return Rig(tmp_path, fake_clock, fake_transport)


def test_observer_beat_file_is_written_each_cycle(rig, fake_clock):
    rig.obs.run_once()
    p = rig.state / "observer.beat"
    first = p.read_text().strip()
    assert first.endswith("Z")
    fake_clock.advance(60)
    rig.obs.run_once()
    assert p.read_text().strip() != first


def test_stopped_plugin_goes_to_manager_only(rig):
    rig.register("sl-work-tdd")
    rig.tl.set_rows("plugins", {"work-tdd": {"mode": "run"}})
    rig.beat("work-tdd", age_s=400)
    rig.obs.run_once()
    a = rig.alerts("liveness")
    assert [(e["data"]["target"], e["data"]["destination"]) for e in a] == [("work-tdd", "manager")]
    assert rig.requests() == []  # プラグインは再起動の要求を出さない


def test_stopped_core_goes_to_host_and_manager_and_writes_restart_request(rig):
    rig.register("timeline", "ledger")
    rig.beat("timeline", age_s=10)
    rig.beat("ledger", age_s=400)
    rig.obs.run_once()
    a = rig.alerts("liveness")
    assert sorted(e["data"]["destination"] for e in a) == ["host", "manager"]
    assert all(e["data"]["target"] == "ledger" for e in a)
    names = rig.requests()
    assert len(names) == 1
    body = json.loads((rig.state / "host-requests" / names[0]).read_text())
    assert body == {"op": "restart", "name": "ledger"}


def test_restart_request_is_not_duplicated_while_pending(rig, fake_clock):
    rig.register("ledger")
    rig.beat("ledger", age_s=400)
    rig.obs.run_once()
    fake_clock.advance(31 * 60)  # 警報の窓を過ぎて再度出る。未処理の要求は重ねない
    rig.beat("ledger", age_s=2000)
    rig.obs.run_once()
    assert len(rig.requests()) == 1
    assert len(rig.alerts("liveness")) == 4  # 2 回 × 宛先 2


def test_same_alert_once_within_dedup_window_then_again(rig, fake_clock):
    rig.register("sl-a")
    rig.tl.set_rows("plugins", {"a": {"mode": "run"}})
    rig.beat("a", age_s=999)
    for _ in range(5):
        rig.obs.run_once()
        fake_clock.advance(60)
    assert len(rig.alerts("liveness")) == 1
    fake_clock.advance(30 * 60)
    rig.obs.run_once()
    assert len(rig.alerts("liveness")) == 2


def test_dedup_table_survives_restart(rig, tmp_path, fake_clock, fake_transport):
    rig.register("sl-a")
    rig.tl.set_rows("plugins", {"a": {}})
    rig.beat("a", age_s=999)
    rig.obs.run_once()
    from core.observer.dedup import DedupTable

    t = DedupTable(rig.data / "dedup.json", 30)
    assert t.allow("liveness", "a") is False


def test_unregistered_and_commander_names_are_not_watched(rig):
    rig.s.commander_units = ("paseo-example",)
    rig.register("paseo-example")  # 司令塔の名前は登録があっても対象外
    rig.beat("paseo-example", age_s=9999)
    rig.obs.run_once()
    assert rig.alerts() == []


def test_never_beaten_registered_component_alerts_after_threshold(rig, fake_clock):
    rig.register("sl-new")
    rig.tl.set_rows("plugins", {"new": {}})
    rig.obs.run_once()
    assert rig.alerts("liveness") == []  # 起動直後は猶予
    fake_clock.advance(200)
    rig.obs.run_once()
    assert [e["data"]["target"] for e in rig.alerts("liveness")] == ["new"]


def test_timeline_down_falls_back_to_healthz_probe(rig):
    rig.register("ledger", "control")
    rig.probe_ok["ledger"] = False
    rig.tl.stop()
    rig.obs.run_once()
    assert rig.obs.timeline_down is True
    # 事象は時系列記録に届かないので outbox に溜まる。ホストへの要求は届く
    names = rig.requests()
    bodies = [json.loads((rig.state / "host-requests" / n).read_text()) for n in names]
    assert {"op": "restart", "name": "ledger"} in bodies
    assert {"op": "restart", "name": "control"} not in bodies
    rig.tl.start()
    rig.sink.flush()
    assert any(e["data"]["target"] == "ledger" for e in rig.alerts("liveness"))


def test_expired_pending_goes_to_issuer_and_manager_issued_goes_to_backup(rig, fake_clock):
    from datetime import timedelta

    from tests.b8.helpers import fmt

    old = fmt(fake_clock.now() - timedelta(minutes=20))
    recent = fmt(fake_clock.now() - timedelta(minutes=5))
    rig.tl.set_rows("controls", {
        "c1": {"state": "expired_pending", "expires_at": old, "issued_by": "manager"},
        "c2": {"state": "expired_pending", "expires_at": old, "issued_by": "inspection"},
        "c3": {"state": "expired_pending", "expires_at": recent, "issued_by": "manager"},  # 猶予の中
        "c4": {"state": "active", "expires_at": old, "issued_by": "manager"},
    })
    called = []
    rig.obs.set_hook("backup", lambda a: called.append(a))
    rig.obs.run_once()
    got = {e["data"]["target"]: e["data"]["destination"] for e in rig.alerts("expired_pending")}
    assert got == {"c1": "backup", "c2": "inspection"}
    assert len(called) == 1 and called[0].target == "c1"
    assert len(rig.tl.of_kind("backup.activated")) == 1


def test_backup_without_hook_records_alert_but_not_activation(rig, fake_clock):
    from datetime import timedelta

    from tests.b8.helpers import fmt

    rig.tl.set_rows("controls", {"c1": {"state": "expired_pending", "issued_by": "manager",
                                        "expires_at": fmt(fake_clock.now() - timedelta(hours=1))}})
    rig.obs.run_once()
    assert len(rig.alerts("expired_pending")) == 1
    assert rig.tl.of_kind("backup.activated") == []
    assert rig.obs.health_extra()["failed"].get("backup_unavailable") == 1


def test_lag_over_threshold_goes_to_manager(rig):
    rig.register("timeline")
    rig.beat("timeline", lag_s=301)
    rig.beat("ledger", lag_s=10)
    rig.obs.run_once()
    assert [(e["data"]["target"], e["data"]["destination"]) for e in rig.alerts("lag")] == [("timeline", "manager")]


def test_starved_role_queue(rig, fake_clock):
    from datetime import timedelta

    from tests.b8.helpers import fmt

    old = fmt(fake_clock.now() - timedelta(minutes=90))
    fresh = fmt(fake_clock.now() - timedelta(minutes=5))
    rig.tl.set_rows("jobs", {
        "j1": {"state": "queued", "role": "impl", "last_ts": old},
        "j2": {"state": "completed", "role": "impl", "last_ts": old},
        "j3": {"state": "queued", "role": "review", "last_ts": old},
        "j4": {"state": "running", "role": "review", "last_ts": fresh},  # 進んでいる
        "j5": {"state": "completed", "role": "test", "last_ts": old},  # 待ちが無い
    })
    rig.obs.run_once()
    assert [e["data"]["target"] for e in rig.alerts("starve")] == ["impl"]


def test_repeat_error_over_threshold_per_hour(rig):
    for _ in range(6):
        rig.tl.add("error.classified", "ledger", {"class": "system.expected", "code": "disk_busy"})
    for _ in range(5):
        rig.tl.add("error.classified", "ledger", {"class": "system.expected", "code": "slow"})  # 5 回は超えない
    rig.obs.run_once()
    assert [e["data"]["target"] for e in rig.alerts("repeat")] == ["ledger:disk_busy"]


def test_repeat_window_drops_old_errors(rig, fake_clock):
    for _ in range(6):
        rig.tl.add("error.classified", "ledger", {"class": "system.expected", "code": "x"})
    fake_clock.advance(61 * 60)
    rig.obs.run_once()
    assert rig.alerts("repeat") == []


def test_manager_silent_goes_to_backup(rig, fake_clock):
    rig.register("manager")
    rig.beat("manager")
    rig.tl.add("manager.cycle", "manager", {"trigger": "tick"})
    rig.obs.run_once()
    assert rig.alerts("manager_trouble") == []
    fake_clock.advance(31 * 60)
    rig.beat("manager")
    rig.obs.run_once()
    a = rig.alerts("manager_trouble")
    assert [(e["data"]["target"], e["data"]["destination"]) for e in a] == [("silent", "backup")]


def test_manager_not_expected_before_it_is_registered(rig, fake_clock):
    fake_clock.advance(3600)
    rig.obs.run_once()
    assert rig.alerts("manager_trouble") == []


def test_manager_bad_streak_and_reset_by_decision(rig):
    rig.register("manager")
    rig.beat("manager")
    rig.tl.add("manager.cycle", "manager", {})
    for _ in range(2):
        rig.tl.add("error.classified", "manager", {"class": "artifact.bug", "code": "bad_output"})
    rig.obs.run_once()
    assert rig.alerts("manager_trouble") == []
    rig.tl.add("error.classified", "manager", {"class": "artifact.bug", "code": "bad_output"})
    rig.obs.run_once()
    assert [e["data"]["target"] for e in rig.alerts("manager_trouble")] == ["bad_streak"]
    # 成功した判断で連続が切れる
    rig.tl.add("manager.decision", "manager", {})
    rig.obs.run_once()
    assert rig.obs.memory.bad_outputs == 0


def test_unknown_event_unhandled_then_handled(rig, fake_clock):
    rig.register("manager")
    rig.beat("manager")
    u = rig.tl.add("error.classified", "ledger", {"class": "unknown", "code": "weird"})
    rig.tl.add("manager.cycle", "manager", {})
    rig.obs.run_once()
    fake_clock.advance(31 * 60)
    rig.tl.add("manager.cycle", "manager", {})  # 管理役は動いているが unknown は扱っていない
    rig.beat("manager")
    rig.obs.run_once()
    assert [e["data"]["target"] for e in rig.alerts("manager_trouble")] == ["unknown_unhandled"]
    rig.tl.add("manager.decision", "manager", {"event_ids": [u["id"]]})
    rig.obs.run_once()
    assert rig.obs.memory.unknown_pending == {}


def test_drain_silent_asks_inspector_to_resume(rig, fake_clock):
    got = []
    rig.obs.set_hook("inspection", lambda a: got.append(a.target))
    rig.tl.add("inspection.phase", "inspector", {"phase": "draining"})
    rig.obs.run_once()
    assert rig.alerts("drain_silent") == []
    fake_clock.advance(11 * 60)
    rig.obs.run_once()
    a = rig.alerts("drain_silent")
    assert len(a) == 1 and a[0]["data"]["destination"] == "inspection"
    assert got == ["inspection"]


def test_drain_not_silent_when_manager_active_or_not_draining(rig, fake_clock):
    rig.tl.add("inspection.phase", "inspector", {"phase": "draining"})
    rig.obs.run_once()
    fake_clock.advance(8 * 60)
    rig.tl.add("manager.decision", "manager", {})
    fake_clock.advance(8 * 60)
    rig.obs.run_once()
    assert rig.alerts("drain_silent") == []
    rig.tl.add("inspection.phase", "inspector", {"phase": "notice"})
    fake_clock.advance(60 * 60)
    rig.obs.run_once()
    assert rig.alerts("drain_silent") == []


def test_watch_check_is_not_implemented_without_signal_and_others_continue(rig):
    rig.register("sl-a")
    rig.tl.set_rows("plugins", {"a": {}})
    rig.beat("a", age_s=999)
    rig.obs.run_once()
    assert "watch" in rig.obs.health_extra()["unimplemented_checks"]
    assert len(rig.alerts("liveness")) == 1  # 他の検査は動く


def test_watch_check_with_signal(rig):
    rig.obs.watch_signal = lambda: [{"target": "sl-x", "severity": "high", "detail": "d"}]
    rig.obs.run_once()
    a = rig.alerts("watch")
    assert [(e["data"]["target"], e["data"]["destination"]) for e in a] == [("sl-x", "manager")]


def test_one_failing_check_does_not_stop_the_others(rig):
    def boom(ctx):
        raise RuntimeError("x")

    rig.obs.checks.insert(0, ("boom", boom))
    rig.register("sl-a")
    rig.tl.set_rows("plugins", {"a": {}})
    rig.beat("a", age_s=999)
    r = rig.obs.run_once()
    assert rig.obs.failed["boom"] == 1
    assert len(rig.alerts("liveness")) == 1
    assert any(x.startswith("boom:") for x in r["errors"])


def test_added_check_runs(rig):
    rig.obs.add_check("mine", lambda ctx: [Alert("mine", "t", ("manager",))])
    rig.obs.run_once()
    assert len(rig.alerts("mine")) == 1


def test_all_spec_checks_are_present():
    names = [n for n, _ in DEFAULT_CHECKS]
    assert names == ["liveness", "watch", "expired_pending", "lag", "starve", "repeat", "manager_trouble", "drain_silent"]


def test_observer_app_exposes_only_healthz_and_loads_ext(rig, tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from core.observer.app import create_observer_app

    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "bx.py").write_text("def register(app):\n    app.state.observer.set_hook('backup', lambda a: None)\n")
    app = create_observer_app(rig.obs, ext_dir=str(ext), run_loop=False)
    with TestClient(app) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["component"] == "observer" and "backup" in body["hooks"]
        assert c.get("/v1/events").status_code == 404


def test_beat_updates_the_same_inode_for_a_file_bind(rig, fake_clock):
    rig.obs.run_once()
    path = rig.state / "observer.beat"
    inode = path.stat().st_ino
    fake_clock.advance(60)
    rig.obs.run_once()
    assert path.stat().st_ino == inode


def test_observer_ledger_client_uses_core_secret(tmp_path, monkeypatch, fake_transport):
    from core.observer.app import build_observer
    from core.observer.settings import Settings
    directory = tmp_path / "secrets"
    directory.mkdir()
    token = "fake-observer-token"
    (directory / "core-token-observer").write_text(token)
    monkeypatch.setenv("AX_SECRETS_PATH", str(directory))
    obs = build_observer(Settings(state_dir=str(tmp_path), data_dir=str(tmp_path / "data")))
    fake_transport.add("POST", "/v1/todos", status=201, json={"ok": True})
    obs.hostlog.ledger._transport = fake_transport
    obs.hostlog.ledger.create_todo({"title": "fake"})
    assert fake_transport.calls[-1].headers["Authorization"] == "Bearer " + token


def test_manager_decisions_do_not_hide_a_stalled_cycle(rig, fake_clock):
    rig.register("manager")
    rig.beat("manager")
    rig.tl.add("manager.cycle", "manager", {})
    rig.obs.run_once()
    fake_clock.advance(31 * 60)
    rig.beat("manager")
    rig.tl.add("manager.decision", "manager", {})
    rig.obs.run_once()
    assert any(e["data"]["target"] == "silent" for e in rig.alerts("manager_trouble"))


def test_restart_rebuilds_recent_error_history(rig):
    from core.observer.observer import Observer
    for _ in range(6):
        rig.tl.add("error.classified", "ledger", {"class": "system.expected", "code": "slow"})
    rig.obs.run_once()
    restarted = Observer(rig.s, rig.obs.timeline, rig.sink, ledger=rig.obs.hostlog.ledger, sources=rig.obs.sources)
    restarted.run_once()
    assert len(restarted.memory.errors) == 6
    assert len(rig.alerts("repeat")) == 1


def test_partial_feed_failure_keeps_already_read_events(rig):
    from axcommon.client import Unavailable
    original = rig.obs.feed.poll
    def poll(kind, tail):
        if kind == "manager.cycle":
            raise Unavailable("fake")
        return original(kind, tail)
    rig.obs.feed.poll = poll
    rig.tl.add("error.classified", "ledger", {"class": "unknown", "code": "weird"})
    rig.obs.run_once()
    assert len(rig.obs.memory.unknown_pending) == 1

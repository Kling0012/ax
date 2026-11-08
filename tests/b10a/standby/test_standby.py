from __future__ import annotations

import json
import threading
from datetime import timedelta
from types import ModuleType, SimpleNamespace

import pytest

from axcommon import schemas, timeutil
from axcommon.client import LedgerClient, TimelineClient, Unavailable
from axcommon.events import EventSink
from core.observer.checks import Ctx, check_manager_trouble
from core.observer.ext import b10a
from core.observer.model import Alert
from core.observer.observer import Observer
from core.observer.settings import Settings
from core.observer.standby import REASON_CARD, REASON_ROOT, REASON_TOP
from core.observer.standby.backup import Standby
from core.observer.standby.cycle import CycleRequest, ManagerModuleRunner
from core.observer.standby.frequency import count_activations
from core.observer.standby.rootcause import TodoSender, root_cause_todo
from tests.fakes.b10a.fake_manager import FakeManager
from tests.fakes.b10a.world import World


@pytest.fixture
def rig(tmp_path, fake_clock):
    world = World()
    ledger = LedgerClient("http://ledger:8080", token="dummy", transport=world)
    timeline = TimelineClient("http://timeline:8080", transport=world)
    sink = EventSink(timeline, "observer", "fake-host", tmp_path / "events")
    manager = FakeManager()
    standby = Standby(runner=manager, ledger=ledger, timeline=timeline, sink=sink,
                      data_dir=tmp_path / "standby", inline=True)
    return SimpleNamespace(world=world, ledger=ledger, timeline=timeline, sink=sink,
                           manager=manager, standby=standby, clock=fake_clock, path=tmp_path)


def alert():
    return Alert("manager_trouble", "silent", ("backup",), "high", "manager.cycle がない")


def roots(rig):
    return [t for t in rig.world.ledger.todos.values() if t.get("reason") in (REASON_ROOT, REASON_TOP)]


def test_t14_manager_stops_and_observer_calls_backup_once(rig):
    settings = Settings(state_dir=str(rig.path), data_dir=str(rig.path / "observer"))
    obs = Observer(settings, rig.timeline, rig.sink)
    obs.memory.started = rig.clock.now() - timedelta(minutes=settings.manager_silent_min + 1)
    obs.set_hook("backup", rig.standby.on_alert)
    obs.add_check("standby_drain", rig.standby.drain_check)
    ctx = Ctx(settings, obs.sources, rig.clock.now(), obs.memory, {}, {"manager": 60})
    alerts = check_manager_trouble(ctx)
    assert len(alerts) == 1
    obs.deliver(alerts[0])
    request, = rig.manager.requests
    assert (request.role, request.issued_by, request.emit_cycle) == ("backup_manager", "backup", False)
    assert request.trigger == {"check": "manager_trouble", "target": "silent", "detail": alerts[0].detail}
    activation, = rig.world.timeline.of_kind("backup.activated")
    assert activation["data"]["reason"]
    cycle, = rig.world.timeline.of_kind("manager.cycle")
    assert activation["seq"] < cycle["seq"]
    assert cycle["data"]["issued_by"] == "backup"
    root, = roots(rig)
    assert root["title"].startswith("管理役の不調: ")
    schemas.validate("todo", root)
    assert rig.world.ledger.created_with_token == ["Bearer dummy"]


def test_healthy_manager_does_not_call_backup(rig):
    settings = Settings()
    obs = Observer(settings, rig.timeline, rig.sink)
    obs.memory.last_manager_activity = rig.clock.now()
    ctx = Ctx(settings, obs.sources, rig.clock.now(), obs.memory, {}, {"manager": 60})
    assert check_manager_trouble(ctx) == []
    assert rig.manager.requests == []


@pytest.mark.parametrize("manager", [None, FakeManager(ok=False), FakeManager(raises=NotImplementedError()),
                                     FakeManager(raises=RuntimeError("fake failure"))])
def test_root_is_created_even_when_cycle_fails(rig, manager):
    rig.standby.runner = manager
    result = rig.standby.activate(alert())
    assert result["cycle"] != "ok"
    assert len(roots(rig)) == 1
    assert rig.standby.cycle_failures == 1


def test_module_runner_uses_exact_contract(monkeypatch):
    module = ModuleType("fake_cycle")
    calls = []

    def run_cycle(**kw):
        calls.append(kw)
        return {"ok": True, "actions": [{"type": "noop"}], "cycle_id": "fake-cycle"}

    module.run_cycle = run_cycle
    monkeypatch.setitem(__import__("sys").modules, "fake_cycle", module)
    result = ManagerModuleRunner("fake_cycle").run(CycleRequest(trigger={"check": "silent"}))
    assert calls == [{"role": "backup_manager", "issued_by": "backup", "trigger": {"check": "silent"}, "emit_cycle": False}]
    assert result.ok and result.actions == 1 and result.cycle_id == "fake-cycle"


@pytest.mark.parametrize("name", ["fake_missing_module", "fake_missing_function"])
def test_missing_manager_contract_stays_not_implemented(monkeypatch, name):
    if name == "fake_missing_function":
        monkeypatch.setitem(__import__("sys").modules, name, ModuleType(name))
    with pytest.raises(NotImplementedError):
        ManagerModuleRunner(name).run(CycleRequest(trigger={}))


def test_frequency_at_limit_one_root_per_activation_and_user_card(rig):
    first = rig.standby.activate(alert())
    second = rig.standby.activate(alert())
    assert first["count"] == 1 and not first["frequent"]
    assert second["count"] == 2 and second["frequent"]
    assert len(roots(rig)) == 2
    assert [t["reason"] for t in roots(rig)] == [REASON_ROOT, REASON_TOP]
    card, = [t for t in rig.world.ledger.todos.values() if t.get("reason") == REASON_CARD]
    assert card["owner"] == "user"
    schemas.validate("todo", card)
    rig.standby.activate(alert())
    assert len(roots(rig)) == 3
    assert roots(rig)[-1]["reason"] == REASON_TOP
    assert sum(t.get("reason") == REASON_CARD for t in rig.world.ledger.todos.values()) == 1


def test_window_boundary_and_expired_activations(rig):
    rig.standby.activate(alert())
    rig.clock.advance(7 * 86400)
    assert count_activations(timeline=None, log=rig.standby.log, now=rig.clock.now(), days=7) == 1
    rig.clock.advance(.001)
    result = rig.standby.activate(alert())
    assert result["count"] == 1 and not result["frequent"]


def test_frequency_reads_timeline_and_counts_paginated_events(rig, monkeypatch):
    from core.observer.standby import frequency

    monkeypatch.setattr(frequency, "PAGE", 2)
    old = timeutil.format_ts(rig.clock.now() - timedelta(days=8))
    rig.world.timeline.add("backup.activated", ts=old)
    rig.world.timeline.add("backup.activated")
    rig.world.timeline.add("health.alert")
    rig.world.timeline.add("backup.activated")
    result = rig.standby.activate(alert())
    assert result["count"] == 2 and result["frequent"]


def test_timeline_outage_uses_local_count_and_recovers_events(rig):
    rig.world.timeline.down()
    for _ in range(2):
        rig.sink.emit("backup.activated", "fake", {"reason": "silent"})
        result = rig.standby.activate(alert())
    assert result["count"] == 2 and result["frequent"]
    rig.world.timeline.up()
    rig.sink.flush()
    assert len(rig.world.timeline.of_kind("backup.activated")) == 2
    assert count_activations(timeline=rig.timeline, log=rig.standby.log, now=rig.clock.now(), days=7) == 2


def test_ledger_outage_replays_same_ids_after_restart(rig):
    rig.world.ledger.down()
    rig.standby.activate(alert())
    rig.standby.activate(alert())
    rows = rig.standby.todos.read_pending()
    assert len(rows) == 3
    ids = [row["todo"]["id"] for row in rows]
    restarted = Standby(runner=rig.manager, ledger=rig.ledger, timeline=rig.timeline, sink=rig.sink,
                        data_dir=rig.path / "standby", inline=True)
    assert restarted.drain_check(None) == []
    assert restarted.todos.pending_count() == 3
    rig.world.ledger.up()
    assert restarted.drain_check(None) == []
    assert list(rig.world.ledger.todos) == ids
    assert restarted.todos.pending_count() == 0
    restarted.drain_check(None)
    assert list(rig.world.ledger.todos) == ids
    assert not restarted.activate(alert())["frequent"]


def test_response_lost_then_conflict_does_not_duplicate_todo(rig):
    original = rig.ledger.create_todo
    todo = root_cause_todo(check="manager_trouble", target="silent")

    def accepted_but_lost(body):
        original(body)
        raise Unavailable("fake response lost")

    rig.ledger.create_todo = accepted_but_lost
    assert rig.standby.todos.send(todo) == "queued"
    rig.ledger.create_todo = original
    assert rig.standby.todos.drain() == 1
    assert list(rig.world.ledger.todos) == [todo["id"]]
    assert rig.standby.todos.pending_count() == 0


def test_rejected_todo_is_retained_without_repeated_delivery(rig):
    rig.world.ledger.reject_create_status = 422
    result = rig.standby.activate(alert())
    assert result["root_cause"] == "rejected"
    assert rig.standby.todos.pending_count() == 0
    rejected = [json.loads(line) for line in rig.standby.todos.rejected.read_text().splitlines()]
    assert rejected[0]["todo"]["id"] == result["root_cause_id"]
    assert rig.standby.todos.drain() == 0


def test_concurrent_drain_preserves_newly_queued_todo(tmp_path):
    entered, release, writer_started, writer_done = (threading.Event() for _ in range(4))
    first = root_cause_todo(check="manager_trouble", target="first")
    second = root_cause_todo(check="manager_trouble", target="second")
    sender = TodoSender(None, tmp_path)
    sender.send(first)

    def post(todo):
        if todo["id"] == first["id"]:
            entered.set()
            assert release.wait(3)
            return "created"
        raise Unavailable("fake outage")

    sender._post = post
    errors = []

    def drain():
        try:
            sender.drain()
        except Exception as exc:
            errors.append(exc)

    def write():
        writer_started.set()
        try:
            sender.send(second)
        except Exception as exc:
            errors.append(exc)
        finally:
            writer_done.set()

    draining = threading.Thread(target=drain)
    writing = threading.Thread(target=write)
    draining.start()
    try:
        assert entered.wait(3)
        writing.start()
        assert writer_started.wait(3)
        writer_done.wait(.1)
    finally:
        release.set()
        draining.join(3)
        if writing.ident is not None:
            writing.join(3)
    assert not draining.is_alive() and not writing.is_alive()
    assert not errors
    assert [row["todo"]["id"] for row in sender.read_pending()] == [second["id"]]


def test_extension_registers_backup_and_drain(rig, monkeypatch):
    obs = Observer(Settings(state_dir=str(rig.path), data_dir=str(rig.path / "observer")), rig.timeline, rig.sink)
    app = SimpleNamespace(state=SimpleNamespace(observer=obs))
    monkeypatch.setattr(b10a, "build_standby", lambda observer: rig.standby)
    b10a.register(app)
    assert app.state.standby is rig.standby
    obs.deliver(alert())
    assert len(rig.manager.requests) == 1
    drain, = [fn for name, fn in obs.checks if name == "standby_drain"]
    assert drain(None) == []


def test_alert_at_worker_exit_starts_another_worker(rig):
    rig.standby.inline = False
    finished, release = threading.Event(), threading.Event()
    original = rig.standby._loop
    threads = []

    def delayed_exit():
        threads.append(threading.current_thread())
        original()
        finished.set()
        release.wait(3)

    rig.standby._loop = delayed_exit
    rig.standby.on_alert(alert())
    try:
        assert finished.wait(3)
        rig.standby.on_alert(alert())
        assert rig.standby.wait_idle(1)
        assert len(rig.manager.requests) == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
    assert all(not thread.is_alive() for thread in threads)


def test_drain_stops_at_outage_and_keeps_remaining_order(rig):
    rig.world.ledger.down()
    rig.standby.activate(alert())
    rig.standby.activate(alert())
    queued = [row["todo"]["id"] for row in rig.standby.todos.read_pending()]
    rig.world.ledger.up()
    original = rig.ledger.create_todo

    def first_only(todo):
        if todo["id"] == queued[0]:
            return original(todo)
        raise Unavailable("fake outage")

    rig.ledger.create_todo = first_only
    assert rig.standby.todos.drain() == 1
    assert [row["todo"]["id"] for row in rig.standby.todos.read_pending()] == queued[1:]
    rig.ledger.create_todo = original
    assert rig.standby.todos.drain() == 2
    assert list(rig.world.ledger.todos) == queued


def test_drain_quarantines_rejection_and_continues(rig):
    rig.world.ledger.down()
    rig.standby.activate(alert())
    rig.world.ledger.up()
    rig.world.ledger.reject_create_status = 422
    assert rig.standby.todos.drain() == 1
    assert rig.standby.todos.pending_count() == 0
    assert rig.standby.todos.rejected.exists()


def test_build_standby_uses_defaults_and_shared_clients(rig):
    obs = Observer(Settings(state_dir=str(rig.path), data_dir=str(rig.path / "observer")), rig.timeline, rig.sink)
    standby = b10a.build_standby(obs, runner=rig.manager, ledger=rig.ledger,
                                defaults={"backup_window_days": 3, "backup_limit": 4})
    assert standby.runner is rig.manager
    assert standby.timeline is rig.timeline
    assert standby.sink is rig.sink
    assert standby.todos.ledger is rig.ledger
    assert standby.dir == rig.path / "observer" / "standby"
    assert (standby.window_days, standby.limit) == (3, 4)


def test_async_queue_serializes_alerts_and_is_reusable(rig):
    rig.standby.inline = False
    for _ in range(4):
        rig.standby.on_alert(alert())
    assert rig.standby.wait_idle(3)
    assert len(rig.manager.requests) == 4
    rig.standby.on_alert(alert())
    assert rig.standby.wait_idle(3)
    assert len(rig.manager.requests) == 5

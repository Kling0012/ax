"""T-15: snapshot -> release -> resume, preserving user commands and completed operations."""
import json
from pathlib import Path

import pytest

from axcommon import schemas
from core.intake.dest.b10a.runner import EmergencyError, EmergencyRunner, SudoProbe
from core.observer import hostreq


def test_t15_snapshot_precedes_reset_and_resume(emergency):
    w, runner = emergency.world, emergency.runner
    done = w.timeline.add("external.completed", {"operation": "push", "remote_id": "already-done"})
    done = dict(w.timeline.events[0])
    todo = w.ledger.put(title="remain", state="running")
    commands = [w.control.add_command(issuer, "alpha") for issuer in ("manager", "backup", "inspection")]
    held = w.control.add_command("user", "beta", args={"mode": "draining"})
    commander = w.control.add_command("commander", "alpha", op="set_knob", args={"name": "interval", "value": 20})
    result = runner.run("all", "t15")
    assert schemas.errors("ext/b10a/emergency-run.schema.json", result) == []
    assert result["snapshot"]["complete"] is True
    archive = Path(emergency.cfg.data_dir) / result["snapshot"]["path"]
    before = json.loads((archive / "commands.json").read_text())
    assert all(c["state"] == "active" for c in before["active"]["commands"])
    assert {c["id"] for c in before["active"]["commands"]} == {*commands, held, commander}
    assert json.loads((archive / "todos.json").read_text())["todos"] == [todo]
    assert json.loads((archive / "jobs.json").read_text())["jobs"] == w.pool.jobs
    export = json.loads((archive / "timeline-export.json").read_text())
    assert export["files"] == ["timeline-export.jsonl"]
    assert export["events"] == [done]
    assert w.timeline.exports[0] == [done]
    assert set(result["reset"]["released"]) == set(commands)
    assert w.control.commands[held]["state"] == "active"
    assert w.control.commands[commander]["state"] == "active"
    assert all(token == "Bearer test-emergency" for _, token in w.control.released)
    assert result["resume"]["plugins"]["alpha"]["at_default"] is True
    assert result["resume"]["plugins"]["beta"] == {
        "mode": "draining", "default_mode": "stopped", "at_default": False, "held_by": ["user"]}
    events = [e["kind"] for e in w.timeline.events]
    assert events.index("emergency.snapshot") < events.index("emergency.reset") < events.index("emergency.resumed")
    assert w.timeline.events[0] == done
    assert w.ledger.todos[todo["id"]] == todo
    assert w.pool.jobs[0]["state"] == "running"
    assert set(hostreq.pending(emergency.cfg.requests_dir)) == {
        ("restart", name) for name in ("manager", "inspector", "timer-runner", "sl-alpha", "sl-beta", "hq", "intake", "timeline", "observer", "unrelated")}
    for path in emergency.cfg.requests_dir.glob("*.req"):
        assert set(json.loads(path.read_text())) == {"op", "name"}
    assert all(host in {"timeline", "control", "ledger", "worker-pool"} for host, _, _ in w.calls)
    assert w.paths("worker-pool", "POST") == []


def test_plugin_scope_keeps_other_plugins_and_global_commands(emergency):
    w = emergency.world
    mine = w.control.add_command("manager", "alpha", state="expired_pending")
    other = w.control.add_command("inspection", "beta")
    global_ = w.control.add_command("inspection", "all")
    user = w.control.add_command("user", "alpha", args={"mode": "stopped"})
    result = emergency.runner.run("alpha")
    assert result["reset"]["released"] == [mine]
    assert all(w.control.commands[c]["state"] == "active" for c in (other, global_, user))
    assert result["reset"]["restart"]["requested"] == ["sl-alpha"]
    assert set(result["resume"]["plugins"]) == {"alpha"}


def test_commanders_only_releases_commander_without_restarts(emergency):
    w = emergency.world
    commander = w.control.add_command("commander", "alpha")
    manager = w.control.add_command("manager", "alpha")
    user = w.control.add_command("user", "alpha")
    result = emergency.runner.run("commanders")
    assert result["reset"]["released"] == [commander]
    assert w.control.commands[manager]["state"] == w.control.commands[user]["state"] == "active"
    assert result["reset"]["restart"]["skipped"] == "not_applicable"
    assert result["resume"]["plugins"] == {}
    assert hostreq.pending(emergency.cfg.requests_dir) == []


def test_sudo_unavailable_skips_only_restart(emergency):
    w = emergency.world
    cmd = w.control.add_command("backup", "alpha")
    Path(emergency.cfg.state_dir, "hostwatch.log").write_text('{"kind":"host.sudo_unavailable"}\n')
    result = emergency.runner.run("all")
    assert result["reset"]["released"] == [cmd]
    assert result["reset"]["restart"] == {"requested": [], "skipped": "sudo_unavailable"}
    assert result["resume"]["plugins"]["alpha"]["at_default"]
    assert hostreq.pending(emergency.cfg.requests_dir) == []
    with Path(emergency.cfg.state_dir, "hostwatch.log").open("a") as log:
        log.write('invalid\n{"kind":"host.sudo_restored"}\n')
    assert SudoProbe(emergency.cfg.state_dir).available() is True
    assert emergency.runner.run("all")["reset"]["restart"]["requested"]


def test_unknown_sudo_writes_requests_once(emergency):
    assert SudoProbe(emergency.cfg.state_dir).available() is None
    emergency.runner.run("alpha")
    emergency.runner.run("alpha")
    assert hostreq.pending(emergency.cfg.requests_dir) == [("restart", "sl-alpha")]


@pytest.mark.parametrize("part", ["timeline", "ledger", "pool"])
def test_snapshot_dependency_failure_stays_local(emergency, part):
    w = emergency.world
    cmd = w.control.add_command("manager", "alpha")
    getattr(w, part).down()
    result = emergency.runner.run("all")
    assert not result["snapshot"]["complete"]
    assert result["reset"]["released"] == [cmd]
    assert result["resume"]["plugins"]["alpha"]["at_default"]
    if part == "timeline":
        assert emergency.sink.queued_total >= 3
        w.timeline.up()
        emergency.sink.flush()
    assert len(w.timeline.of_kind("emergency.resumed")) == 1


def test_control_unavailable_is_reported_and_other_sections_survive(emergency):
    emergency.world.control.down()
    result = emergency.runner.run("all")
    assert result["reset"]["listing_failed"] is True
    assert result["resume"]["unavailable"] is True
    assert result["snapshot"]["sections"]["todos"]["ok"]
    assert result["reset"]["restart"]["requested"]


def test_archive_write_failure_does_not_prevent_reset(emergency):
    Path(emergency.cfg.data_dir).write_text("not a directory")
    cmd = emergency.world.control.add_command("manager", "alpha")
    result = emergency.runner.run("all", "unwritable")
    assert result["snapshot"]["path"] is None
    assert result["snapshot"]["complete"] is False
    assert result["reset"]["released"] == [cmd]


def test_release_failure_does_not_stop_other_releases(emergency, monkeypatch):
    w = emergency.world
    blocked = w.control.add_command("manager", "alpha")
    good = w.control.add_command("backup", "alpha")
    old = w.control.handle

    def handle(method, path, q, body, headers):
        if path == f"/v1/commands/{blocked}/release":
            return 503, {}, b'{"error":{"code":"unavailable"}}'
        return old(method, path, q, body, headers)

    monkeypatch.setattr(w.control, "handle", handle)
    result = emergency.runner.run("all")
    assert result["reset"]["release_failed"] == [blocked]
    assert result["reset"]["released"] == [good]
    assert len(w.timeline.of_kind("emergency.resumed")) == 1


def test_distrust_command_query_never_releases_user(emergency, monkeypatch):
    w = emergency.world
    user = w.control.add_command("user", "alpha")
    original = emergency.runner.control.get

    def get(path, **kwargs):
        if path == "/v1/commands":
            return {"commands": [w.control.commands[user]]}
        return original(path, **kwargs)

    monkeypatch.setattr(emergency.runner.control, "get", get)
    result = emergency.runner.run("all")
    assert result["reset"]["released"] == []
    assert w.control.commands[user]["state"] == "active"


def test_replay_persists_across_runner_recreation(emergency):
    first = emergency.runner.run("alpha", "same")
    old = emergency.runner
    restored = EmergencyRunner(old.cfg, timeline=old.timeline, control=old.control, ledger=old.ledger,
                               pool=old.pool, sink=old.sink)
    before = list(emergency.world.calls)
    assert restored.run("alpha", "same") == {**first, "replayed": True}
    assert emergency.world.calls == before
    with pytest.raises(EmergencyError) as caught:
        restored.run("beta", "same")
    assert (caught.value.status, caught.value.code) == (409, "idempotency_mismatch")


def test_same_timestamp_produces_distinct_snapshots(emergency):
    a = emergency.runner.run("alpha", "a")
    b = emergency.runner.run("beta", "b")
    assert a["snapshot"]["path"] != b["snapshot"]["path"]
    for result in (a, b):
        manifest = Path(emergency.cfg.data_dir, result["snapshot"]["path"], "manifest.json")
        assert json.loads(manifest.read_text())["scope"] == result["scope"]


def test_busy_does_not_start_second_run(emergency):
    emergency.runner._lock.acquire()
    try:
        with pytest.raises(EmergencyError) as caught:
            emergency.runner.run("all")
        assert caught.value.code == "busy"
        assert emergency.world.calls == []
    finally:
        emergency.runner._lock.release()


@pytest.mark.parametrize("scope,status", [("unknown", 404), ("../alpha", 422), ("", 422)])
def test_bad_scope_has_no_side_effects(emergency, scope, status):
    with pytest.raises(EmergencyError) as caught:
        emergency.runner.run(scope)
    assert caught.value.status == status
    assert emergency.world.paths("control", "POST") == []
    assert emergency.world.timeline.events == []


def test_snapshot_redacts_synthetic_private_data(emergency):
    private = "sk-" + "A" * 30
    emergency.world.ledger.put(title=private)
    result = emergency.runner.run("all")
    text = Path(emergency.cfg.data_dir, result["snapshot"]["path"], "todos.json").read_text()
    assert private not in text
    assert "[REDACTED]" in text


def test_redactor_failure_stops_only_snapshot_writes(emergency):
    def broken(data):
        raise RuntimeError("scanner unavailable")

    emergency.runner._redact = broken
    cmd = emergency.world.control.add_command("inspection", "alpha")
    result = emergency.runner.run("all")
    assert result["snapshot"]["complete"] is False
    assert result["snapshot"]["path"] is None
    assert result["reset"]["released"] == [cmd]
    assert not list(Path(emergency.cfg.data_dir, "archive").rglob("*.json"))


def test_timeline_snapshot_pages_to_fixed_export_boundary(emergency, monkeypatch):
    w = emergency.world
    for n in range(450):
        w.timeline.add("external.completed", {"number": n})
    get = emergency.runner.timeline.get_events
    cursors = []

    def growing(**kwargs):
        cursors.append(kwargs["after"])
        w.timeline.add("external.completed", {"number": "later"})
        return get(**kwargs)

    monkeypatch.setattr(emergency.runner.timeline, "get_events", growing)
    result = emergency.runner.run("all")
    exported = json.loads(Path(emergency.cfg.data_dir, result["snapshot"]["path"], "timeline-export.json").read_text())
    assert result["snapshot"]["complete"]
    assert cursors == [0, 200, 400]
    assert exported["last_seq"] == len(exported["events"]) == 450
    assert all(event["data"]["number"] != "later" for event in exported["events"])


def test_missing_timeline_page_marks_snapshot_incomplete(emergency, monkeypatch):
    emergency.world.timeline.add("external.completed")
    monkeypatch.setattr(emergency.runner.timeline, "get_events", lambda **kw: {"events": []})
    cmd = emergency.world.control.add_command("manager", "alpha")
    result = emergency.runner.run("all")
    assert not result["snapshot"]["complete"]
    assert not result["snapshot"]["sections"]["timeline-export"]["ok"]
    assert result["reset"]["released"] == [cmd]

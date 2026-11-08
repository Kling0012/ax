"""b17-1 acceptance boundaries; all operations stay in tmp_path."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from axcommon.ids import new_ulid
from tools.cutover.common import (Authority, CommandRunner, Context, CutoverError, GateBlocked,
                                  IntegrationPending, Profile, latest_result)
from tools.cutover.files import (copy_old_todo, copy_originals, copy_preserving_original,
                                  place_entry, remove_old_entry, without_pending_guard)
from tools.cutover.stages import (ACTIONS, Monitoring, Operator, REAL_TESTS, RETIRED_TESTS,
                                  feature_states, notification_accepted)
from tests.fakes.b17.world import FakeBackend, FakeRunner


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AX_PUBLISH_LEGACY_PLUGIN_IDS", "legacy-publisher")
    profile = Profile("fixture", tmp_path / "state", tmp_path / "tmp")
    runner = FakeRunner(profile.record_path)
    go = tmp_path / "GO.md"
    go.write_text("scope alone is not authority", encoding="utf-8")
    ctx = Context(profile, tmp_path / "archive", runner, False, Authority(go, new_ulid()))
    backend = FakeBackend()
    return ctx, runner, backend, Operator(ctx, backend)


def test_T70_default_dry_run_has_no_effects(world, monkeypatch):
    ctx, runner, backend, _ = world
    dry = Context(ctx.profile, ctx.archive_dir)
    operator = Operator(dry, backend)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("subprocess executed"))
    for stage in range(1, 7):
        outcome = operator.advance(stage)
        assert outcome.result == "planned" and outcome.dry_run
    assert backend.calls == [] and runner.calls == []
    assert not ctx.archive_dir.exists() and not ctx.profile.record_path.exists()
    assert CommandRunner().run(["forbidden"]).planned


@pytest.mark.parametrize("mode", ["no_go", "unverified", "no_claim", "missing_change", "malformed_owner"])
def test_T70_go_file_and_design_agreement_are_insufficient(world, mode):
    ctx, runner, backend, operator = world
    if mode == "no_go":
        ctx.authority = None
    elif mode == "unverified":
        runner.verify_ok = False
    elif mode == "no_claim":
        runner.holder = False
    elif mode == "missing_change":
        runner.approved = []
    else:
        runner.owner = {"principal": "forged"}
    with pytest.raises(GateBlocked):
        operator.advance(1)
    assert "copy_originals" not in backend.calls
    assert not ctx.profile.record_path.exists()


def test_T70_withdrawn_go_stops_next_action(world):
    ctx, _, backend, operator = world
    backend.conditions["go_not_withdrawn"] = False
    with pytest.raises(GateBlocked):
        operator.advance(1)
    assert "copy_originals" not in backend.calls


def test_T71_stage1_and_stage2_order(world):
    ctx, _, backend, operator = world
    operator.advance(1)
    operator.advance(2)
    actions = [name for name in backend.calls if not name.startswith("check:")]
    assert actions == list(ACTIONS[1]) + list(ACTIONS[2])
    assert actions.index("start_monitoring") < actions.index("start_core") < actions.index("place_entry")
    assert "remove_old_entry" not in actions and "retire_others" not in actions
    assert latest_result(ctx.records(), "orig-copy") == "ok"
    assert latest_result(ctx.records(), "entry-placed") == "ok"


@pytest.mark.parametrize("condition", ["monitoring_normal", "ax_status_200", "entry_usable"])
def test_T71_failed_stage2_never_removes_or_restarts_old(world, condition):
    _, _, backend, operator = world
    backend.conditions[condition] = False
    with pytest.raises(GateBlocked):
        operator.advance(2)
    assert "remove_old_entry" not in backend.calls and "retire_others" not in backend.calls
    assert not any("rollback" in call or "restart_old" in call for call in backend.calls)


def make_originals(home):
    originals = {".claude/settings.json": json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "python3 pending-guard.py"},
        {"type": "command", "command": "python3 dev-flow-hook.py stop"}]}]}}),
        ".claude/CLAUDE.md": "old roles original@example.invalid\n" "@RTK.md\n", ".claude/RTK.md": "rtk\n", ".claude.json": "{}",
        ".claude/rules/roles/worker.md": "worker", ".claude/rules/topics/models-routing.md": "models",
        ".claude/hub/ROLE.md": "role", ".claude/projects/p/memory/MEMORY.md": "memory",
        ".claude/rules/topics/storage.md": "# storage\n- 削除しない。archive-to-hdd が compute-host へ運ぶ。\n"}
    for relative, text in originals.items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return originals


def test_T71_originals_preserved_and_safe_hook_retained(world, tmp_path):
    ctx, _, _, _ = world
    home = tmp_path / "home"
    originals = make_originals(home)
    copy_originals(ctx, home)
    guide = tmp_path / "generated.md"
    guide.write_text("ax work; ax status guide@example.invalid\n" "@RTK.md\n", encoding="utf-8")
    place_entry(ctx, home, guide)
    remove_old_entry(ctx, home, new_entry_usable=True)
    settings = json.loads((home / ".claude/settings.json").read_text())
    commands = settings["hooks"]["Stop"][0]["hooks"]
    assert commands == [{"type": "command", "command": "python3 dev-flow-hook.py stop"}]
    assert (ctx.archive_dir / "settings/settings.json").read_text() == originals[".claude/settings.json"]
    assert (ctx.archive_dir / "claude-md/CLAUDE.md").read_text() == originals[".claude/CLAUDE.md"]
    assert (ctx.archive_dir / "claude-md/RTK.md").read_text() == "rtk\n"
    assert (ctx.archive_dir / "memory-index/p/MEMORY.md").read_text() == "memory"
    assert (ctx.archive_dir / "mcp/.claude.json").read_text() == "{}"
    assert (home / ".claude.json").read_text() == "{}"  # F-5.4: gateway registration is retained.
    assert "@RTK.md" in (home / ".claude/CLAUDE.md").read_text()
    assert not (home / ".claude/rules/roles").exists()
    assert (ctx.archive_dir / "rules/removed/rules/roles/worker.md").read_text() == "worker"
    remove_old_entry(ctx, home, new_entry_usable=True)  # Already removed is idempotent.


def test_T71_snapshot_conflicts_never_overwrite(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_text("original")
    copy_preserving_original(src, dst)
    src.write_text("changed")
    with pytest.raises(CutoverError):
        copy_preserving_original(src, dst)
    assert dst.read_text() == "original" and src.read_text() == "changed"


def test_T71_hook_matching_does_not_remove_unrelated_command():
    settings = {"hooks": {"Stop": [{"hooks": [
        {"command": "echo pending-guard.py"}, {"command": "rtk hook claude"},
        {"command": "python3 -I /path/pending-guard.py"}]}]}}
    cleaned = without_pending_guard(settings)
    assert len(cleaned["hooks"]["Stop"][0]["hooks"]) == 2
    assert len(settings["hooks"]["Stop"][0]["hooks"]) == 3


def test_T72_T75_retired_only():
    assert RETIRED_TESTS == ("T-72", "T-75")
    assert not set(RETIRED_TESTS).intersection(REAL_TESTS)
    assert not any("restore" in a or "dependency" in a for actions in ACTIONS.values() for a in actions)


def test_T73_old_sqlite_is_copied_not_imported(world, tmp_path):
    ctx, _, _, _ = world
    source = tmp_path / "todo.sqlite"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE todos(title TEXT)")
        conn.execute("INSERT INTO todos VALUES ('legacy')")
    copy_old_todo(ctx, source, writer_stopped=True)
    assert source.exists()
    with sqlite3.connect(ctx.archive_dir / "todo/todo.sqlite") as conn:
        assert conn.execute("SELECT title FROM todos").fetchall() == [("legacy",)]
    with pytest.raises(CutoverError):
        copy_old_todo(ctx, source, writer_stopped=False)


def test_T74_old_todo_card_is_one_idempotent_request(world):
    ctx, _, backend, operator = world
    operator.advance(4)
    operator.advance(4)
    assert not any("import" in call for call in backend.calls)
    assert backend.calls.count("create_old_todo_card") == 2
    assert backend.ledger_cards == {"cutover/old-todo": "旧 ToDo の扱いを決める"}
    assert latest_result(ctx.records(), "patrol") == "waiting"


def test_T76_T77_collect_inventory_before_direct_patrol(world):
    ctx, _, backend, operator = world
    ctx.note("T-78", "T-78/fixture", "ok")
    assert operator.advance(4).result == "ok"
    assert backend.calls.index("collect_inventory") < backend.calls.index("resume_patrol")
    assert latest_result(ctx.records(), "cutover-time") == "ok"
    assert backend.modes == {"social-network": "collect_only", "chat-service": "collect_only", "scheduled-publisher": "collect_only"}
    assert not any("legacy_queue" in a or "parallel" in a or "ramp" in a for a in backend.calls)


@pytest.mark.parametrize("condition", ["running_jobs_zero", "prerequisites_registered", "monitoring_normal",
                                       "entry_usable", "b17_passed_except_t78"])
def test_T82_pre_stop_conditions_are_checked_before_retirement(world, condition):
    _, _, backend, operator = world
    backend.conditions[condition] = False
    with pytest.raises(GateBlocked):
        operator.advance(4)
    assert "retire_others" not in backend.calls


@pytest.mark.parametrize("condition", ["old_components_stopped", "inventory_recorded", "cutover_time_visible",
                                       "inventory_mutations_zero"])
def test_T82_post_stop_conditions_prevent_patrol(world, condition):
    ctx, _, backend, operator = world
    ctx.note("T-78", "T-78/fixture", "ok")
    backend.conditions[condition] = False
    with pytest.raises(GateBlocked):
        operator.advance(4)
    assert "retire_others" in backend.calls and "resume_patrol" not in backend.calls


@pytest.mark.parametrize("result", [None, "failed"])
def test_T78_exception_allows_other_retirement_but_not_patrol_or_reap(world, result):
    ctx, _, backend, operator = world
    if result:
        ctx.note("T-78", "T-78/fixture", result)
    assert operator.advance(4).result == "waiting"
    assert "retire_others" in backend.calls
    assert "retire_archive_to_hdd" not in backend.calls
    assert "resume_patrol" not in backend.calls
    with pytest.raises(GateBlocked):
        operator.advance(6)
    assert "enable_reap" not in backend.calls


def test_T78_latest_failed_result_invalidates_old_success(world):
    ctx, _, backend, operator = world
    ctx.note("T-78", "T-78/fixture", "ok")
    ctx.note("T-78", "T-78/fixture", "failed")
    assert operator.advance(4).result == "waiting"


def test_T55_acceptance_does_not_wait_for_delivery_answer():
    assert notification_accepted(200, "message-id")
    assert not notification_accepted(200, "")
    assert not notification_accepted(500, "message-id")


@pytest.mark.parametrize("test", [test for test in REAL_TESTS if test != "T-78"])
def test_T58_T80_T215_all_other_real_tests_block_before_route_and_removal(world, test):
    ctx, _, backend, operator = world
    backend.results[test] = "not_run"
    with pytest.raises(GateBlocked):
        operator.advance(2)
    assert "route_hq" not in backend.calls and "retire_others" not in backend.calls
    assert latest_result(ctx.records(), "entry-placed") is None


def test_T79_no_external_service_management_and_monitoring_first(world):
    _, _, backend, operator = world
    operator.advance(2)
    actions = [a for a in backend.calls if not a.startswith("check:")]
    assert actions[0] == "start_monitoring"
    assert not any(name in str(actions) for name in ("Paseo", "Cloudflare", "ntfy-server"))


def test_T94_feature_credentials_disable_only_that_feature():
    state = feature_states({"Steam": False, "X": False, "Discord": True, "scheduled-publisher": False, "Jev": False})
    assert state["Discord"]["enabled"]
    assert not state["X"]["enabled"] and state["X"]["reason"] == "credential_unverified"
    assert "feature_credentials" not in str(ACTIONS)


def test_public_browser_failure_does_not_gate_retirement(world):
    _, _, backend, operator = world
    backend.outputs["check_public_browser"] = False
    assert operator.advance(5).result == "waiting"
    assert operator.advance(4).result == "waiting"  # Only T-78 is missing.
    assert "retire_others" in backend.calls
    assert "cutover/hq-access-email" in backend.todos


def test_monitoring_four_conditions_include_archive_init_exception():
    monitoring = Monitoring(10, 30, {"observer": ("running", None), "archive-init": ("exited", 0)},
                            (10, 20), 10, 3, 60, 30, 0, True, True)
    assert monitoring.ok()
    for invalid in (replace(monitoring, observer_age_s=31), replace(monitoring, observed_s=59),
                    replace(monitoring, new_alerts=1), replace(monitoring, heartbeat_ages_s=(31,)),
                    replace(monitoring, services={"observer": ("exited", 0)}),
                    replace(monitoring, manager_healthy=False), replace(monitoring, backup_healthy=False)):
        assert not invalid.ok()


def test_note_failure_stops_following_steps_without_restarting_old(world):
    _, runner, backend, operator = world
    runner.note_ok = False
    with pytest.raises(CutoverError):
        operator.advance(2)
    assert "route_hq" not in backend.calls
    assert not any("restart_old" in call for call in backend.calls)


def test_missing_integration_never_starts_partial_stage(world):
    _, _, backend, operator = world
    backend.missing.add("split_memory")
    with pytest.raises(IntegrationPending):
        operator.advance(3)
    assert "remove_old_entry" not in backend.calls


def test_cli_defaults_to_plan_without_importing_adapter(world, monkeypatch, capsys):
    from tools.cutover.__main__ import main
    ctx, _, _, _ = world
    monkeypatch.setattr(Profile, "load", lambda path: ctx.profile)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("subprocess executed"))
    assert main(["--profile", "dummy.yaml", "--archive", str(ctx.archive_dir), "--stage", "4",
                 "--adapter", "does.not.exist:factory"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "planned" and result["dry_run"] is True
    assert not ctx.archive_dir.exists()


def test_cli_apply_needs_go_before_loading_adapter(world, monkeypatch, capsys):
    from tools.cutover.__main__ import main
    ctx, _, _, _ = world
    monkeypatch.setattr(Profile, "load", lambda path: ctx.profile)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("subprocess executed"))
    assert main(["--profile", "dummy.yaml", "--archive", str(ctx.archive_dir), "--stage", "1", "--apply",
                 "--adapter", "does.not.exist:factory"]) == 3
    assert json.loads(capsys.readouterr().out)["result"] == "blocked"


def test_stage6_requires_retirement_before_enabling_reap(world):
    ctx, _, backend, operator = world
    ctx.note("T-78", "T-78/fixture", "ok")
    backend.conditions["archive_to_hdd_stopped"] = False
    with pytest.raises(GateBlocked):
        operator.advance(6)
    assert "enable_reap" not in backend.calls
    backend.conditions["archive_to_hdd_stopped"] = True
    assert operator.advance(6).result == "ok"
    assert latest_result(ctx.records(), "reap-enabled") == "ok"
    assert backend.calls.index("enable_reap") < backend.calls.index("copy_cutover_log")


def test_record_reader_never_uses_missing_identity_or_claim(world):
    ctx, _, _, _ = world
    ctx.note("T-78", "T-78/fixture", "failed")
    forged = dict(ctx.records()[0])
    forged["ts"] = "2099-01-01T00:00:00.000Z"
    forged["source"] = {"component": "cutover-operator"}
    forged["data"] = {"result": "ok"}
    with ctx.profile.record_path.open("a") as stream:
        stream.write(json.dumps(forged) + "\n")
    assert latest_result(ctx.records(), "T-78") == "failed"


def test_bound_backend_requires_each_fresh_observation(world):
    from tools.cutover.bindings import BoundBackend
    ctx, _, _, _ = world
    backend = BoundBackend(actions={name: lambda ctx: None for name in ACTIONS[1]})
    with pytest.raises(IntegrationPending):
        Operator(ctx, backend).advance(1)

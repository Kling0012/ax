"""T-78/T-125/T-154 on a temporary host; no network or service commands."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from axcommon import ids, schemas, timeutil
from core.archiver.config import Source
from core.archiver.engine import Archiver
from core.archiver.guard import Guard
from core.archiver.ledger import ArchiverLedger
from core.archiver.manifest import build_state, write_manifest
from tests.fakes.b17.reap.parts import FakeOperator, LocalTransfer
from tools.cutover.reap import Paths, ReapOperator
from tools.cutover.reap.common_adapter import from_context

NAMES = ("mp-runs-archive", "ci-public-logs", "gh-auto-wt", "codex-runs", "claude-projects")


@pytest.fixture
def world(tmp_path):
    paths = Paths(tmp_path / "state", tmp_path / "profile" / "archive-sources.yaml",
                  tmp_path / "cutover-archive", tmp_path / "docs" / "CHANGELOG.md", "test-host")
    paths.sources_file.parent.mkdir()
    sources = []
    files = {}
    for name in NAMES:
        root = tmp_path / "sources" / name
        root.mkdir(parents=True)
        target = root / "old.log"
        target.write_bytes(b"old test record\n")
        old = (timeutil.now() - timedelta(days=15)).timestamp()
        os.utime(target, (old, old))
        files[name] = target
        source = {"name": name, "path": str(root), "min_age_days": 7, "remove_original": False}
        if name == "claude-projects":
            source["keep_original"] = ["*/memory/**"]
        sources.append(source)
        state = build_state(root, target.name)
        write_manifest(paths.verified / name / "2026-01-01.json", name, "2026-01-01", [state])
    paths.sources_file.write_text(yaml.safe_dump({"sources": sources}, sort_keys=False))
    fake = FakeOperator(paths)
    fake.note("cutover.state", "T-78", "T-78/test-host", "ok", "fixture_checksum_verified")
    return paths, fake, ReapOperator(paths, fake.ports()), files


def reap_cycle(repo_root, paths, monkeypatch, capsys, hook=None):
    """Execute the unmodified host script's isolated Python payload locally.

    Bash only does argv parsing, initial marker/lock guards and python -I. This
    payload runs with the test interpreter so the existing cached PyYAML is used.
    No subprocess, systemd, shell service, network, or real archive is touched.
    """
    body = (repo_root / "host" / "ax-archive-reap.sh").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    monkeypatch.setenv("AX_REAP_STATE_DIR", str(paths.state_dir))
    monkeypatch.setenv("AX_REAP_SOURCES", str(paths.sources_file))
    monkeypatch.setenv("AX_REAP_MAX", "1000")
    monkeypatch.setenv("AX_REAP_DRY", "0")
    if hook:
        original = os.lstat

        def lstat(path, *args, **kwargs):
            hook(Path(path))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", lstat)
    exec(compile(body, "host/ax-archive-reap.sh:python", "exec"), {"__name__": "__main__"})
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_t125_enable_only_after_old_stopped(world):
    paths, fake, operator, files = world
    original = paths.sources_file.read_bytes()
    fake.active = True
    result = operator.enable(t2_ok=True)
    assert result.result == "waiting"
    assert paths.sources_file.read_bytes() == original
    assert not paths.marker.exists()
    assert fake.restarts == 0
    assert all(f.exists() for f in files.values())
    fake.active = False
    fake.enabled = True
    assert operator.enable(t2_ok=True).result == "waiting"
    assert paths.sources_file.read_bytes() == original
    assert not paths.marker.exists()


@pytest.mark.parametrize("missing", ["t2", "t78", "manifest", "bad_manifest"])
def test_t125_missing_prerequisite_is_copy_only(world, missing):
    paths, fake, operator, files = world
    if missing == "t78":
        fake.note("cutover.state", "T-78", "T-78/test-host", "failed", "checksum_mismatch")
    if missing == "manifest":
        p = next((paths.verified / "codex-runs").glob("*.json"))
        p.rename(p.with_suffix(".held"))
    if missing == "bad_manifest":
        next((paths.verified / "codex-runs").glob("*.json")).write_text('{"name":"codex-runs","files":[]}')
    original = paths.sources_file.read_bytes()
    assert operator.enable(t2_ok=missing != "t2").result == "waiting"
    assert paths.sources_file.read_bytes() == original
    assert not paths.marker.exists()
    assert not fake.restarts
    assert all(f.exists() for f in files.values())


def test_t125_enabled_four_sources_and_one_time(world):
    paths, fake, operator, _ = world
    before = paths.sources_file.read_bytes()
    result = operator.enable(t2_ok=True)
    assert result.result == "ok", result
    assert paths.marker.read_bytes() == b""
    assert paths.marker.stat().st_mode & 0o777 == 0o600
    assert (result.archive / "archive-sources.yaml").read_bytes() == before
    sources = yaml.safe_load(paths.sources_file.read_text())["sources"]
    assert {s["name"] for s in sources if s["remove_original"]} == set(NAMES[:-1])
    assert "reap enabled" in paths.changelog.read_text()
    assert operator.enable(t2_ok=True).result == "skipped"
    assert fake.restarts == 1
    assert len(paths.changelog.read_text().splitlines()) == 2


@pytest.mark.parametrize("failure", ["restart", "claim_lost", "old_reactivated"])
def test_t125_failed_restart_or_changed_gate_keeps_marker_absent(world, failure):
    paths, fake, operator, files = world
    if failure == "restart":
        fake.restart_ok = False
    elif failure == "claim_lost":
        fake.after_restart = lambda: setattr(fake, "claim", False)
    else:
        fake.after_restart = lambda: setattr(fake, "active", True)
    assert operator.enable(t2_ok=True).result in {"failed", "waiting"}
    assert not paths.marker.exists()
    assert all(p.exists() for p in files.values())


def test_t125_marker_absent_keeps_all_even_if_remove_true(world, repo_root, monkeypatch, capsys):
    paths, _, _, files = world
    doc = yaml.safe_load(paths.sources_file.read_text())
    for item in doc["sources"]:
        item["remove_original"] = True
    paths.sources_file.write_text(yaml.safe_dump(doc))
    result = reap_cycle(repo_root, paths, monkeypatch, capsys)
    assert result["removed"] == 0
    assert all(p.exists() for p in files.values())


def test_t125_reap_retains_changed_and_claude_projects(world, repo_root, monkeypatch, capsys):
    paths, _, operator, files = world
    assert operator.enable(t2_ok=True).result == "ok"
    files["ci-public-logs"].write_bytes(b"changed after verify, no longer the same file")
    result = reap_cycle(repo_root, paths, monkeypatch, capsys)
    assert result["removed"] == 3
    assert result["changed"] == 1
    assert files["ci-public-logs"].exists()
    assert files["claude-projects"].exists()


def test_t125_keep_original_even_if_remove_true(world, repo_root, monkeypatch, capsys):
    paths, _, operator, files = world
    assert operator.enable(t2_ok=True).result == "ok"
    root = files["claude-projects"].parent
    memory = root / "a" / "memory" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("preserved memory")
    state = build_state(root, "a/memory/MEMORY.md", ["*/memory/**"])
    assert state.keep is True
    write_manifest(paths.verified / "claude-projects" / "2026-01-01.json", "claude-projects", "2026-01-01", [state])
    doc = yaml.safe_load(paths.sources_file.read_text())
    next(s for s in doc["sources"] if s["name"] == "claude-projects")["remove_original"] = True
    paths.sources_file.write_text(yaml.safe_dump(doc))
    result = reap_cycle(repo_root, paths, monkeypatch, capsys)
    assert result["keep"] == 1
    assert memory.read_text() == "preserved memory"


def test_t154_stop_snapshot_before_move_and_note(world, monkeypatch):
    paths, fake, operator, _ = world
    assert operator.enable(t2_ok=True).result == "ok"
    original = {str(p.relative_to(paths.verified)): p.read_bytes() for p in paths.verified.rglob("*.json")}
    rename = Path.rename

    def rename_check(path, target):
        if path == paths.marker:
            assert all((target.parent / "verified" / rel).read_bytes() == data for rel, data in original.items())
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", rename_check)
    result = operator.stop(reason="emergency")
    assert result.result == "ok" and result.recorded
    assert not paths.marker.exists()
    assert (result.archive / "reap-enabled").read_bytes() == b""
    assert fake.notes[-1]["subject"] == "reap-stop"
    schemas.validate("event", fake.notes[-1])
    assert operator.enable(t2_ok=True).result == "skipped"


def test_t154_copy_failure_does_not_move_marker(world, monkeypatch):
    import shutil
    paths, _, operator, _ = world
    assert operator.enable(t2_ok=True).result == "ok"

    def fail(*args, **kwargs):
        raise OSError("fake copy failed")

    monkeypatch.setattr(shutil, "copytree", fail)
    assert operator.stop(reason="inconsistency").result == "failed"
    assert paths.marker.exists()


@pytest.mark.parametrize("role", ["manager", "backup", "inspector"])
def test_t154_claim_required_for_non_operator(world, role):
    paths, fake, operator, _ = world
    assert operator.enable(t2_ok=True).result == "ok"
    fake.claim = False
    assert operator.stop(reason="emergency").reason == "cutover_claim_required"
    assert paths.marker.exists()
    assert operator.enable(t2_ok=True).reason == "cutover_claim_required"
    assert operator.resume(paths.cutover_archive / "reap-stop-forged").reason == "cutover_claim_required"


def test_t154_normal_round_cannot_stop(world):
    paths, _, operator, _ = world
    assert operator.enable(t2_ok=True).result == "ok"
    assert operator.stop(reason="normal_round").result == "skipped"
    assert paths.marker.exists()


def test_t154_stop_then_resume_rechecks_current_files(world, repo_root, monkeypatch, capsys):
    paths, fake, operator, files = world
    assert operator.enable(t2_ok=True).result == "ok"
    stopped = operator.stop(reason="inconsistency")
    assert stopped.result == "ok"
    assert reap_cycle(repo_root, paths, monkeypatch, capsys)["removed"] == 0
    assert all(f.exists() for f in files.values())
    files["mp-runs-archive"].write_text("changed while stopped")
    assert operator.resume(stopped.archive).result == "ok"
    assert fake.notes[-1]["subject"] == "reap-resume"
    assert paths.marker.exists() and not (stopped.archive / "reap-enabled").exists()
    result = reap_cycle(repo_root, paths, monkeypatch, capsys)
    assert result["removed"] == 3 and result["changed"] == 1
    assert files["mp-runs-archive"].exists()
    assert files["claude-projects"].exists()


def test_t154_stop_during_cycle_checks_marker_for_each_file(world, repo_root, monkeypatch, capsys):
    paths, _, operator, files = world
    assert operator.enable(t2_ok=True).result == "ok"
    first = files["ci-public-logs"]
    changed = files["codex-runs"]
    changed.write_text("changed after verification")
    stopped = []

    def hook(path):
        if path == first and not stopped:
            stopped.append(True)
            assert operator.stop(reason="emergency").result == "ok"

    result = reap_cycle(repo_root, paths, monkeypatch, capsys, hook)
    assert result["removed"] <= 1
    assert changed.exists()
    assert files["mp-runs-archive"].exists()
    assert not paths.marker.exists()


def test_t154_note_failure_is_not_success(world):
    paths, fake, operator, _ = world
    assert operator.enable(t2_ok=True).result == "ok"
    fake.notes_ok = False
    stopped = operator.stop(reason="emergency")
    assert stopped.result == "failed" and stopped.reason == "stopped_note_failed"
    assert not paths.marker.exists()
    assert (stopped.archive / "reap-enabled").exists()


@pytest.mark.parametrize("mode", ["ok", "same_size_mismatch", "down", "unverifiable", "local_changed"])
def test_t78_content_checksum_result_and_original_retained(world, tmp_path, mode):
    paths, fake, operator, files = world
    transfer = LocalTransfer(tmp_path / "remote")
    source = files["claude-projects"]
    transfer.corrupt = mode == "same_size_mismatch"
    transfer.down = mode == "down"
    transfer.unverifiable = mode == "unverifiable"
    if mode == "local_changed":
        transfer.after_verify = lambda: source.write_bytes(b"changed after verify")
    result = operator.verify_t78(transfer, relative_path=source.name)
    assert result.result == ("ok" if mode == "ok" else "failed")
    assert result.recorded
    assert source.exists()
    assert not paths.marker.exists()
    event = fake.notes[-1]
    assert event["subject"] == "T-78" and event["data"]["result"] == result.result
    schemas.validate("event", event)
    if mode == "same_size_mismatch":
        copied = next((tmp_path / "remote").rglob(source.name))
        assert copied.stat().st_size == source.stat().st_size
        assert copied.read_bytes() != source.read_bytes()


@pytest.mark.parametrize("relative", ["../outside", "/absolute", "x-cookie"])
def test_t78_invalid_probe_is_never_sent(world, tmp_path, relative):
    _, _, operator, files = world
    transfer = LocalTransfer(tmp_path / "remote")
    assert operator.verify_t78(transfer, relative_path=relative).result == "failed"
    assert not transfer.calls
    assert files["claude-projects"].exists()


def test_t78_existing_archiver_keeps_unverified_source(world, tmp_path):
    paths, _, _, files = world
    root = files["mp-runs-archive"].parent
    transfer = LocalTransfer(tmp_path / "remote")
    transfer.corrupt = True
    ledger = ArchiverLedger(":memory:")
    events = []

    class Sink:
        def emit(self, kind, subject, data=None, **kwargs):
            events.append((kind, subject, data))

    engine = Archiver([Source("mp-runs-archive", str(root), root, 7, remove_original=True)], transfer,
                      ledger, Sink(), state_dir=tmp_path / "engine-state", guard=Guard())
    try:
        report = engine.run_once()
        assert report.sources[0].failed == "verify_mismatch"
        assert report.sources[0].removed == 0
        assert files["mp-runs-archive"].exists()
        assert any(e[0] == "archive.failed" for e in events)
        assert not (tmp_path / "engine-state" / "archive" / "verified").exists()
    finally:
        ledger.close()


def test_t154_control_and_timer_have_no_reap_route(tmp_path):
    from core.control import Reject, TIMER_ACTIONS
    from core.control.service import ControlService
    from core.control.store import Store
    from runtime.timer_runner.runner import ACTIONS

    assert not any("reap" in action for action in (*TIMER_ACTIONS, *ACTIONS))
    assert "reap-stop" not in schemas.load("control-command")["properties"]["op"]["enum"]
    store = Store(tmp_path / "control.sqlite")
    try:
        service = ControlService(store, None)
        with pytest.raises(Reject) as rejected:
            service.issue({"target": "reap", "op": "set_mode", "args": {"mode": "poll_paused"},
                           "reason": "test", "issued_by": "user"}, issued_by="user")
        assert rejected.value.code == "unknown_target"
    finally:
        store.close()


def test_common_context_dry_run_cannot_change_any_files(world):
    from tools.cutover.common import Context, Profile
    paths, _, _, _ = world
    ctx = Context(Profile(paths.host_id, paths.state_dir, paths.state_dir.parent), paths.cutover_archive)
    operator = from_context(ctx, sources_file=paths.sources_file, changelog=paths.changelog,
                            restart_argv=["compose-test", "restart", "archiver"])
    before = paths.sources_file.read_bytes()
    assert operator.enable(t2_ok=True).result == "waiting"
    assert paths.sources_file.read_bytes() == before
    assert not paths.marker.exists() and not ctx.runner.calls


def test_common_stage6_calls_reap_and_uses_shared_notes(world, tmp_path):
    from tools.cutover.common import Authority, CommandResult, Context, Profile
    from tools.cutover.reap.common_adapter import ReapBackend
    from tools.cutover.stages import Operator
    from tests.fakes.b17.world import FakeBackend, FakeRunner

    paths, fake, _, _ = world

    class Runner(FakeRunner):
        def run(self, argv, **kwargs):
            if list(argv[:2]) == ["systemctl", "--user"]:
                return fake.run(list(argv))
            if list(argv) == ["compose-test", "restart", "archiver"]:
                return CommandResult(0 if fake.restart() else 1)
            return super().run(argv, **kwargs)

    go = tmp_path / "GO.md"
    go.write_text("fake approval fixture")
    ctx = Context(Profile(paths.host_id, paths.state_dir, tmp_path), paths.cutover_archive,
                  runner=Runner(paths.journal), dry_run=False, authority=Authority(go, ids.new_ulid()))
    reap = from_context(ctx, sources_file=paths.sources_file, changelog=paths.changelog,
                        restart_argv=["compose-test", "restart", "archiver"])
    backend = ReapBackend(FakeBackend(), reap, t2_check=lambda: True)
    result = Operator(ctx, backend).advance(6)
    assert result.result == "ok"
    assert paths.marker.exists() and fake.restarts == 1
    assert any(note["subject"] == "reap-enabled" for note in ctx.notes)
    stopped = reap.stop(reason="emergency")
    assert stopped.recorded
    assert ctx.notes[-1]["subject"] == "reap-stop"
    assert reap.resume(stopped.archive).recorded
    assert ctx.notes[-1]["subject"] == "reap-resume"
    backend.t2_check = lambda: False
    # Already enabled is idempotent and is never a second restart.
    assert Operator(ctx, backend).advance(6).result == "ok"
    assert fake.restarts == 1


def test_stage6_cannot_report_success_when_enable_gate_failed(world):
    from tools.cutover.common import GateBlocked
    from tools.cutover.reap.common_adapter import ReapBackend
    from tests.fakes.b17.world import FakeBackend

    paths, fake, operator, _ = world
    fake.active = True
    backend = ReapBackend(FakeBackend(), operator, t2_check=lambda: True)
    with pytest.raises(GateBlocked, match="old_archiver_not_stopped"):
        backend.perform("enable_reap", None)
    assert not paths.marker.exists()


def test_enable_failure_after_marker_reports_active_state(world):
    paths, _, operator, _ = world
    paths.changelog.parent.mkdir(parents=True)
    paths.changelog.mkdir()
    result = operator.enable(t2_ok=True)
    assert result.result == "failed"
    assert result.reason == "enable_incomplete_marker_present"
    assert paths.marker.exists()
    assert operator.enable(t2_ok=True).result == "skipped"


@pytest.mark.parametrize("mode", ["ok", "same_size_mismatch", "unrestricted", "no_command", "unknown"])
def test_t78_command_through_public_entry(world, tmp_path, monkeypatch, capsys, mode):
    from subprocess import CompletedProcess
    from core.archiver.config import Dest
    from tools.cutover.common import Profile
    from tools.cutover.reap import t78

    paths, fake, operator, files = world
    profile = tmp_path / "t78.yaml"
    profile.write_text(yaml.safe_dump({
        "ax_profile": str(tmp_path / "ax.yaml"), "sources_file": str(paths.sources_file),
        "secrets_dir": str(tmp_path / "secrets"), "cutover_archive": str(paths.cutover_archive),
        "changelog": str(paths.changelog), "private_key": str(tmp_path / "key"),
        "public_key": str(tmp_path / "key.pub"), "known_hosts": str(tmp_path / "known_hosts"),
        "key_check_host": "archive-host", "authorized_keys": "/root/.ssh/authorized_keys",
        "restricted_directory": "/srv/archive/hub-host",
    }))
    monkeypatch.setattr(t78.Profile, "load", lambda path: Profile("test-host", paths.state_dir, tmp_path))
    monkeypatch.setattr(t78, "load_dest", lambda *a, **k: Dest("archive-host", 22, "archive"))
    monkeypatch.setattr(t78, "from_context", lambda *a, **k: operator)
    transfer = LocalTransfer(tmp_path / "remote")
    transfer.corrupt = mode == "same_size_mismatch"
    monkeypatch.setattr(t78, "Transport", lambda *a, **k: transfer)

    def fake_run(argv, **kwargs):
        if argv[0] == "ssh-keygen":
            return CompletedProcess(argv, 0, "256 SHA256:abc fixture (ED25519)\n", "")
        if mode == "unknown":
            raise OSError("unavailable")
        options = {'unrestricted': 'command="rrsync /srv/archive/hub-host"',
                   'no_command': 'restrict'}.get(mode, 'restrict,command="rrsync /srv/archive/hub-host"')
        return CompletedProcess(argv, 0, options + "\n", "")

    monkeypatch.setattr(t78.subprocess, "run", fake_run)
    status = t78.main(["--profile", str(profile), "--apply", "--go-file", str(tmp_path / "go"), "--approval-id", "fixture"])
    result = json.loads(capsys.readouterr().out)
    assert status == (0 if mode == "ok" else 4)
    assert result["recorded"] is True
    assert fake.notes[-1]["subject"] == "T-78"
    assert files["claude-projects"].exists()
    if mode in {"unrestricted", "no_command", "unknown"}:
        assert result["reason"] == ("restricted_key_unknown" if mode == "unknown" else "restricted_key_missing")
        assert not transfer.calls
    elif mode == "same_size_mismatch":
        copy = next((tmp_path / "remote").rglob("old.log"))
        assert copy.stat().st_size == files["claude-projects"].stat().st_size
        assert copy.read_bytes() != files["claude-projects"].read_bytes()

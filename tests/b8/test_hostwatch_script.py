"""host/ax-hostwatch.sh（bash の入口）を偽の sudo で端から端まで動かす。unit と timer の雛形の検査。実際の sudo・docker は呼ばない。"""
import configparser
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FAKE_SUDO = ROOT / "tests" / "fakes" / "b8" / "fake_sudo.py"


@pytest.fixture
def box(tmp_path):
    prof = yaml.safe_load((ROOT / "profile" / "example.yaml").read_text())
    prof.update(secrets_dir=str(tmp_path / "secrets"), tmp_dir=str(tmp_path / "tmp"), ax_state_dir=str(tmp_path / "state"),
                commander_unit="paseo-test.service")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "state" / "hostwatch" / "registered").mkdir(parents=True)
    (tmp_path / "state" / "deploy").mkdir()
    (tmp_path / "state" / "deploy" / "current.env").write_text("AX_TAG=t0\n")
    home = tmp_path / "axhome"
    (home / "compose.d").mkdir(parents=True)
    (home / "compose.yaml").write_text("name: ax\n")
    pp = tmp_path / "profile.yaml"
    pp.write_text(yaml.safe_dump(prof))
    (tmp_path / "ps.json").write_text(json.dumps({"Service": "timeline", "State": "exited", "Health": "", "Name": "ax-timeline-1"}) + "\n")
    (tmp_path / "services.txt").write_text("timeline\nledger\npaseo-test\n")
    (tmp_path / "state" / "hostwatch" / "registered" / "timeline").touch()
    (tmp_path / "state" / "hostwatch" / "registered" / "paseo-test").touch()

    class B:
        pass

    b = B()
    b.tmp, b.state, b.home, b.profile = tmp_path, tmp_path / "state", home, pp
    b.log = tmp_path / "sudo.log"

    def run(**extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "AX_"))}
        env.update(AX_PROFILE=str(pp), AX_HOME=str(home), AX_HOSTWATCH_SUDO=str(FAKE_SUDO), FAKE_SUDO_LOG=str(b.log),
                   FAKE_PS_OUT=str(tmp_path / "ps.json"), FAKE_SERVICES=str(tmp_path / "services.txt"))
        env.update(extra)
        return subprocess.run(["bash", str(ROOT / "host" / "ax-hostwatch.sh")], capture_output=True, text=True, env=env)

    b.run = run
    b.calls = lambda: [json.loads(x) for x in b.log.read_text().splitlines()] if b.log.exists() else []
    b.events = lambda: [json.loads(x) for x in (b.state / "hostwatch.log").read_text().splitlines()] if (b.state / "hostwatch.log").exists() else []
    return b


def test_cycle_starts_registered_exited_service_via_sudo_and_skips_commander(box):
    r = box.run()
    assert r.returncode == 0, r.stderr
    calls = box.calls()
    assert calls[0] == ["true"]
    ups = [c for c in calls if "up" in c]
    assert len(ups) == 1 and ups[0][:2] == ["docker", "compose"] and ups[0][-4:] == ["up", "--no-deps", "-d", "timeline"]
    assert not any("paseo-test" in " ".join(c) for c in calls if "up" in c or "restart" in c)
    assert "ax-hostwatch:" in r.stderr  # journal へ（標準エラー）
    acts = [e for e in box.events() if e["kind"] == "host.action"]
    assert any(e["data"]["op"] == "up" and e["data"]["target"] == "timeline" and e["data"]["result"] == "ok" for e in acts)


def test_cycle_with_sudo_denied_runs_no_docker_and_logs_sudo_unavailable(box):
    r = box.run(FAKE_SUDO_MODE="deny")
    assert r.returncode == 0
    def assert_denied_calls(calls):
        assert len(calls) == 2 and calls[0] == ["true"]
        assert calls[1][:1] == ["env"] and calls[1][2:5] == ["python3", "-P", "-m"]
        assert calls[1][5] == "axcommon.redact_index"
        assert calls[1][calls[1].index("--secrets-dir") + 1] == str(box.tmp / "secrets")
        assert not any("docker" in call or any("ax-deploy" in arg for arg in call) for call in calls)

    assert_denied_calls(box.calls())
    assert [e["kind"] for e in box.events() if e["kind"].startswith("host.sudo")] == ["host.sudo_unavailable"]
    before = len(box.calls())
    r2 = box.run(FAKE_SUDO_MODE="deny")
    assert r2.returncode == 0
    assert_denied_calls(box.calls()[before:])
    assert [e["kind"] for e in box.events() if e["kind"].startswith("host.sudo")] == ["host.sudo_unavailable"]
    r3 = box.run()
    assert [e["kind"] for e in box.events() if e["kind"].startswith("host.sudo")] == ["host.sudo_unavailable", "host.sudo_restored"]


def test_unreadable_profile_exits_1_without_touching_anything(box):
    box.profile.write_text("not: [valid")
    r = box.run()
    assert r.returncode == 1 and box.calls() == []


def test_restart_request_file_written_by_the_observer_helper_is_accepted(box):
    from core.observer import hostreq

    (box.state / "hostwatch" / "registered" / "ledger").touch()
    hostreq.write_request(box.state / "host-requests", "restart", "ledger")
    box.run(FAKE_PS_OUT=str(box.tmp / "none"))  # ps は空 → 登録済みの物は「コンテナが無い」扱い
    assert any(c[-4:] == ["up", "--no-deps", "-d", "ledger"] for c in box.calls())
    assert list((box.state / "host-requests").glob("*.req")) == []
    assert len(list((box.state / "host-requests" / "done").glob("*.req"))) == 1


# --- unit と timer の雛形 ---
def parse_unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(ROOT / "host" / name, encoding="utf-8")
    return cp


def test_service_unit_is_a_oneshot_that_runs_the_script_as_the_user():
    cp = parse_unit("ax-hostwatch.service")
    assert cp["Service"]["Type"] == "oneshot"
    assert cp["Service"]["ExecStart"].endswith("/host/ax-hostwatch.sh")
    text = (ROOT / "host" / "ax-hostwatch.service").read_text()
    assert "User=root" not in text and "NoNewPrivileges=yes" not in text and "NoNewPrivileges=true" not in text.replace("付けない", "")


def test_timer_unit_runs_the_service_every_host_watch_s():
    from axcommon.defaults import Defaults

    cp = parse_unit("ax-hostwatch.timer")
    assert cp["Timer"]["Unit"] == "ax-hostwatch.service"
    assert cp["Timer"]["OnUnitActiveSec"] == f"{Defaults.load().get('host_watch_s')}s"
    assert cp["Install"]["WantedBy"] == "timers.target"


def test_scripts_are_syntax_clean_and_python_compiles():
    for s in ("ax-hostwatch.sh", "ax-deploy.sh", "ax-env.sh", "ax-up.sh", "ax-redact-index.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "host" / s)]).returncode == 0, s
    for p in list((ROOT / "host" / "lib" / "hostwatch").glob("*.py")) + list((ROOT / "core" / "observer").glob("*.py")) + [ROOT / "tools" / "cutover_note.py"]:
        compile(p.read_text(encoding="utf-8"), str(p), "exec")  # 構文の検査（.pyc を書かない）


def test_index_cli_preserves_old_index_when_source_unreadable(box):
    secrets = box.tmp / "secrets"
    (secrets / "readable").write_text("testsecretvalue123")
    (secrets / "another").write_text("anothervalue456")
    binary = secrets / "binary"
    binary.write_bytes(b"\xff")
    index = box.state / "redact" / "index"
    cmd = ["bash", str(ROOT / "host" / "ax-redact-index.sh"), "--profile", str(box.profile)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0 and "UTF-8 対象外 1 件" in result.stdout
    original = index.read_bytes()
    from axcommon.redact_index import value_hash
    lines = original.decode().splitlines()
    assert {value_hash(lines[0], v) for v in ("testsecretvalue123", "anothervalue456")} <= set(lines[1:])
    inaccessible = secrets / "unreadable"
    inaccessible.write_text("private-test-value")
    inaccessible.chmod(0)
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode != 0 and index.read_bytes() == original
    assert "読み取れない" in result.stderr
    assert "private-test-value" not in result.stderr + result.stdout


def test_hostwatch_continues_other_work_when_index_sudo_fails(box):
    helper = box.tmp / "sudo-index-denied"
    helper.write_text("""#!/usr/bin/python3
import os, sys
if sys.argv[1:2] == ['env']:
    sys.exit(1)
os.execv(os.environ['FAKE_SUDO_ORIGINAL'], [os.environ['FAKE_SUDO_ORIGINAL'], *sys.argv[1:]])
""")
    helper.chmod(0o755)
    index = box.state / "redact" / "index"
    index.parent.mkdir()
    index.write_text("old-salt\n")
    r = box.run(AX_HOSTWATCH_SUDO=str(helper), FAKE_SUDO_ORIGINAL=str(FAKE_SUDO))
    assert r.returncode == 0 and index.read_text() == "old-salt\n"
    actions = [e["data"] for e in box.events() if e["kind"] == "host.action"]
    assert any(a.get("op") == "up" and a.get("result") == "ok" for a in actions)
    assert any(a.get("op") == "redact-index" and a.get("result", "").startswith("failed:") for a in actions)


def test_hostwatch_runs_index_generator_through_configured_sudo(box):
    helper = box.tmp / "sudo-index-allowed"
    helper.write_text("""#!/usr/bin/python3
import os, sys
if sys.argv[1:2] == ['env']:
    os.execvp(sys.argv[1], sys.argv[1:])
os.execv(os.environ['FAKE_SUDO_ORIGINAL'], [os.environ['FAKE_SUDO_ORIGINAL'], *sys.argv[1:]])
""")
    helper.chmod(0o755)
    (box.tmp / "secrets" / "value").write_text("ordinarytestsecret123")
    r = box.run(AX_HOSTWATCH_SUDO=str(helper), FAKE_SUDO_ORIGINAL=str(FAKE_SUDO))
    assert r.returncode == 0
    index = box.state / "redact" / "index"
    from axcommon.redact_index import value_hash
    lines = index.read_text().splitlines()
    assert value_hash(lines[0], "ordinarytestsecret123") in lines[1:]
    actions = [e["data"] for e in box.events() if e["kind"] == "host.action"]
    assert any(a.get("op") == "redact-index" and a.get("result") == "ok" for a in actions)

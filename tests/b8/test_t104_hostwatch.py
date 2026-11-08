"""T-104 ホストの特権操作（S-11.5、S-11.6、D-10.3、F-14.2）: ホスト見張りを偽の実行器で動かし、組み立てたコマンドと分岐を確かめる。

実際の sudo・docker は呼ばない。実機（本物の `sudo -n docker compose` での起動し直し、本物の healthz による登録、
`hub-host-local` の能力 docker の ok / unverified）は未実行（tests/b8/README か status/b8.md の表）。
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tests.b8.hostwatch_helpers import (ROOT, Result, log_events, make_cfg, ps_out, quiet_log, register, services_out, write_request)
from tests.fakes.b8.fake_runner import FakeRunner

from hostwatch.watch import HostWatch, run_cycle


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def iso(self, age_s=0.0):
        dt = datetime.fromtimestamp(self.t - age_s, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@pytest.fixture
def env(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = Clock()
    runner = FakeRunner()
    runner.on("config --services", Result(0, services_out("observer", "timeline", "ledger", "sl-a", "manager")))
    runner.on("ps --all", Result(0, ps_out({})))
    hw = HostWatch(cfg, runner, quiet_log(cfg), clock=clock)

    def beat(age):
        (cfg.state_dir / "observer.beat").write_text(clock.iso(age) + "\n")

    class E:
        pass

    e = E()
    e.cfg, e.clock, e.runner, e.hw, e.beat, e.tmp = cfg, clock, runner, hw, beat, tmp_path
    return e


def set_ps(e, rows):
    e.runner.on("ps --all", Result(0, ps_out(rows)))
    e.hw._ps_loaded = False


def fresh(e):
    """1 周期ぶんの新しい HostWatch（キャッシュを持ち越さない）。"""
    return HostWatch(e.cfg, e.runner, quiet_log(e.cfg), clock=e.clock)


# --- コマンドの組み立て ---
def test_up_command_is_assembled_with_sudo_n_project_and_env_files(env):
    register(env.cfg, "timeline")
    set_ps(env, {"timeline": ("exited", "")})
    env.beat(1)
    fresh(env).cycle()
    up = env.runner.cmds(" up --no-deps -d ")
    assert len(up) == 1
    cmd = up[0]
    assert "sudo -n docker compose -p ax --project-directory " + str(env.cfg.ax_home) in cmd
    assert f"--env-file {env.cfg.state_dir}/deploy/profile.env --env-file {env.cfg.state_dir}/deploy/current.env" in cmd
    assert f"-f {env.cfg.ax_home}/compose.yaml -f {env.cfg.ax_home}/compose.d/b1.yaml -f {env.cfg.ax_home}/compose.d/b8.yaml -- up --no-deps -d timeline" in cmd


def test_sudo_prefix_is_replaceable(env):
    env.cfg.sudo = ("fakesudo", "-x")
    register(env.cfg, "timeline")
    set_ps(env, {"timeline": ("exited", "")})
    env.beat(1)
    fresh(env).cycle()
    assert env.runner.cmds("fakesudo -x docker compose")
    assert env.runner.calls[0] == ["fakesudo", "-x", "true"]


def test_first_call_of_a_cycle_is_sudo_n_true(env):
    fresh(env).cycle()
    assert env.runner.calls[0] == ["sudo", "-n", "true"]


# --- 起動し直しの対象 ---
def test_exited_dead_created_missing_and_unhealthy_are_started(env):
    register(env.cfg, "timeline", "ledger", "sl-a", "manager", "observer")
    set_ps(env, {"timeline": ("exited", ""), "ledger": ("running", "unhealthy"), "sl-a": ("dead", ""), "observer": ("running", "healthy")})
    env.beat(1)
    r = fresh(env).cycle()
    assert sorted(r["started"]) == ["ledger", "manager", "sl-a", "timeline"]  # manager はコンテナが無い
    assert env.runner.cmds(" restart ledger")  # 動いているが不調: restart
    assert env.runner.cmds(" up --no-deps -d timeline") and env.runner.cmds(" up --no-deps -d manager") and env.runner.cmds(" up --no-deps -d sl-a")
    assert not env.runner.cmds("observer")  # 健全な物には触れない


def test_running_and_restarting_services_are_left_alone(env):
    register(env.cfg, "timeline", "ledger")
    set_ps(env, {"timeline": ("running", "healthy"), "ledger": ("restarting", "")})
    env.beat(1)
    assert fresh(env).cycle()["started"] == []
    assert env.runner.privileged() == [c for c in env.runner.privileged() if " ps " in c or "--services" in c]


def test_unregistered_services_are_not_started(env):
    register(env.cfg, "timeline")
    set_ps(env, {"timeline": ("running", "healthy"), "ledger": ("exited", ""), "manager": ("exited", "")})
    env.beat(1)
    fresh(env).cycle()
    assert not env.runner.cmds(" up --no-deps -d ")


def test_commander_unit_is_excluded_by_name_even_if_registered(env):
    env.runner.on("config --services", Result(0, services_out("paseo-test", "timeline")))
    register(env.cfg, "paseo-test", "timeline")
    set_ps(env, {"paseo-test": ("exited", ""), "timeline": ("running", "healthy")})
    env.beat(1)
    fresh(env).cycle()
    assert not any("paseo" in c for c in env.runner.cmds(" up "))
    assert not any("paseo" in c for c in env.runner.cmds(" restart "))


def test_registered_name_that_is_not_a_compose_service_is_ignored(env):
    register(env.cfg, "hostwatch")  # compose にない名前（ホスト見張り自身の印）
    env.beat(1)
    fresh(env).cycle()
    assert not env.runner.cmds(" up ")


def test_compose_ps_failure_starts_nothing(env):
    register(env.cfg, "timeline", "ledger")
    env.runner.on("ps --all", Result(1, "", "cannot connect"))
    env.beat(1)
    r = fresh(env).cycle()
    assert r["started"] == [] and not env.runner.cmds(" up --no-deps -d ")


def test_ps_array_format_is_also_read(env):
    register(env.cfg, "timeline")
    env.runner.on("ps --all", Result(0, ps_out({"timeline": ("exited", "")}, ndjson=False)))
    env.beat(1)
    assert fresh(env).cycle()["started"] == ["timeline"]


# --- オブザーバーの beat ---
def test_stale_observer_beat_restarts_observer(env):
    register(env.cfg, "observer")
    set_ps(env, {"observer": ("running", "healthy")})
    env.beat(env.cfg.observer_stale_s + 5)
    r = fresh(env).cycle()
    assert r["started"] == ["observer"]
    assert env.runner.cmds(" restart observer")


def test_fresh_observer_beat_is_not_restarted(env):
    register(env.cfg, "observer")
    set_ps(env, {"observer": ("running", "healthy")})
    env.beat(10)
    assert fresh(env).cycle()["started"] == []


def test_missing_beat_file_restarts_registered_observer(env):
    register(env.cfg, "observer")
    set_ps(env, {"observer": ("running", "healthy")})
    assert fresh(env).cycle()["started"] == ["observer"]


def test_observer_restart_has_a_cooldown_until_beat_returns(env):
    register(env.cfg, "observer")
    set_ps(env, {"observer": ("running", "healthy")})
    env.beat(500)
    assert fresh(env).cycle()["started"] == ["observer"]
    env.clock.t += 60
    assert fresh(env).cycle()["started"] == []  # 起動し直しの直後は待つ
    env.clock.t += env.cfg.observer_stale_s
    assert fresh(env).cycle()["started"] == ["observer"]


def test_observer_is_handled_first(env):
    register(env.cfg, "timeline", "observer")
    set_ps(env, {"timeline": ("exited", ""), "observer": ("exited", "")})
    env.beat(1)
    fresh(env).cycle()
    ups = [c for c in env.runner.cmds(" up --no-deps -d ")]
    assert ups[0].endswith("up --no-deps -d observer") and ups[1].endswith("up --no-deps -d timeline")


def test_observer_not_registered_is_never_started_for_a_stale_beat(env):
    set_ps(env, {})
    assert fresh(env).cycle()["started"] == []


# --- sudo -n の失敗（fail-soft） ---
def test_sudo_failure_skips_privileged_ops_and_logs_once(env):
    env.runner.on(lambda a: a[:3] == ["sudo", "-n", "true"], Result(1, "", "a password is required"))
    register(env.cfg, "timeline", "observer")
    set_ps(env, {"timeline": ("exited", "")})
    write_request(env.cfg, {"op": "restart", "name": "timeline"})
    write_request(env.cfg, {"op": "deploy"}, "01HZZ0000000000000000000A2.req")
    r1 = fresh(env).cycle()
    r2 = fresh(env).cycle()
    assert r1["sudo"] is False and r2["sudo"] is False
    assert env.runner.privileged() == []  # docker compose も要求の実行も呼ばない
    assert not env.runner.cmds("ax-deploy.sh")
    evs = [e for e in log_events(env.cfg) if e["kind"] == "host.sudo_unavailable"]
    assert len(evs) == 1  # 遷移のときだけ
    assert sorted(p.name for p in env.cfg.requests_dir.glob("*.req")) == ["01HZZ0000000000000000000A1.req", "01HZZ0000000000000000000A2.req"]  # 戻ったら処理する


def test_sudo_restored_resumes_automatically_and_processes_pending_requests(env):
    state = {"ok": False}
    env.runner.on(lambda a: a[:3] == ["sudo", "-n", "true"], lambda a: Result(0 if state["ok"] else 1))
    register(env.cfg, "timeline")
    set_ps(env, {"timeline": ("exited", "")})
    env.beat(1)
    write_request(env.cfg, {"op": "restart", "name": "timeline"})
    fresh(env).cycle()
    state["ok"] = True
    r = fresh(env).cycle()
    assert r["sudo"] is True and r["started"] == ["timeline"]
    assert [e["kind"] for e in log_events(env.cfg) if e["kind"].startswith("host.sudo")] == ["host.sudo_unavailable", "host.sudo_restored"]
    assert any(res == "skipped" for _n, res in r["requests"])  # 同じ周期で起動し直し済みなので重ねない
    assert list(env.cfg.requests_dir.glob("*.req")) == []


def test_unprivileged_work_continues_while_sudo_is_unavailable(env):
    env.runner.on(lambda a: a[:3] == ["sudo", "-n", "true"], Result(1))
    r = fresh(env).cycle()
    assert r["index"] == "ok"  # 索引の再作成は sudo を要さない
    assert env.runner.cmds("ax-redact-index.sh")


# --- 要求ファイル ---
def test_restart_request_for_registered_compose_service_runs(env):
    register(env.cfg, "ledger")
    set_ps(env, {"ledger": ("running", "healthy")})
    env.beat(1)
    p = write_request(env.cfg, {"op": "restart", "name": "ledger"})
    r = fresh(env).cycle()
    assert env.runner.cmds(" restart ledger")
    assert r["requests"] == [(p.name, "ok")]
    assert (env.cfg.requests_dir / "done" / p.name).exists() and not p.exists()  # 削除せず done/ へ移す


@pytest.mark.parametrize("body", [
    {"op": "restart", "name": "nonexistent"},                   # compose のサービスに当たらない
    {"op": "restart", "name": "manager"},                       # compose にはあるが登録がない
    {"op": "restart", "name": "paseo-test"},                    # 司令塔
    {"op": "restart", "name": "ledger", "image": "evil:latest"},  # 許されないキー
    {"op": "restart"},                                          # 名前がない
    {"op": "deploy", "name": "ledger"},                         # deploy に名前は付けない
    {"op": "build"},                                            # 知らない操作
    {"op": "restart", "name": "../etc"},                        # 名前の形が違う
    "not json",
    "[1]",
])
def test_invalid_requests_are_rejected_and_archived_not_executed(env, body):
    register(env.cfg, "ledger", "paseo-test")
    set_ps(env, {"ledger": ("running", "healthy")})
    env.beat(1)
    env.runner.on("config --services", Result(0, services_out("ledger", "manager", "paseo-test")))
    p = write_request(env.cfg, body)
    r = fresh(env).cycle()
    assert r["requests"] == [(p.name, "rejected")]
    assert not env.runner.cmds(" restart ") and not env.runner.cmds("ax-deploy.sh")
    assert (env.cfg.requests_dir / "rejected" / p.name).exists()
    assert any(e["data"].get("result") == "rejected" for e in log_events(env.cfg))


def test_symlink_and_oversized_requests_are_rejected(env):
    register(env.cfg, "ledger")
    set_ps(env, {"ledger": ("running", "healthy")})
    env.beat(1)
    target = env.tmp / "elsewhere.json"
    target.write_text(json.dumps({"op": "restart", "name": "ledger"}))
    (env.cfg.requests_dir / "01HZZ0000000000000000000B1.req").symlink_to(target)
    write_request(env.cfg, json.dumps({"op": "restart", "name": "ledger"}) + " " * 600, "01HZZ0000000000000000000B2.req")
    r = fresh(env).cycle()
    assert [res for _n, res in r["requests"]] == ["rejected", "rejected"]
    assert not env.runner.cmds(" restart ")


def test_temp_files_and_other_suffixes_are_ignored(env):
    register(env.cfg, "ledger")
    set_ps(env, {"ledger": ("running", "healthy")})
    env.beat(1)
    write_request(env.cfg, {"op": "restart", "name": "ledger"}, ".01HZZ.tmp")
    write_request(env.cfg, {"op": "restart", "name": "ledger"}, "note.txt")
    assert fresh(env).cycle()["requests"] == []


def test_deploy_request_runs_ax_deploy_with_ax_home_env(env):
    env.beat(1)
    p = write_request(env.cfg, {"op": "deploy"})
    r = fresh(env).cycle()
    cmd = env.runner.cmds("ax-deploy.sh")
    assert cmd == [f"bash {ROOT}/host/ax-deploy.sh"]
    idx = [" ".join(c) for c in env.runner.calls].index(cmd[0])
    assert env.runner.envs[idx]["AX_HOME"] == str(env.cfg.ax_home)
    assert r["requests"] == [(p.name, "ok")]


def test_deploy_exit_10_is_recorded_as_rejected(env):
    env.runner.on("ax-deploy.sh", Result(10))
    env.beat(1)
    p = write_request(env.cfg, {"op": "deploy"})
    assert fresh(env).cycle()["requests"] == [(p.name, "rejected")]


@pytest.mark.parametrize("present", [False, True])
def test_seat_sync_runs_script_when_present_else_unavailable(env, present):
    env.cfg.repo = env.tmp / "scripts"
    (env.cfg.repo / "host").mkdir(parents=True)
    if present:
        (env.cfg.repo / "host/ax-seat.sh").write_text("#!/bin/sh\nexit 0\n")
    env.beat(1)
    write_request(env.cfg, {"op": "seat-sync"})
    r = fresh(env).cycle()
    assert r["requests"][0][1] == ("ok" if present else "unavailable")
    assert bool(env.runner.cmds("ax-seat.sh")) is present


def test_duplicate_requests_run_once(env):
    register(env.cfg, "ledger")
    set_ps(env, {"ledger": ("running", "healthy")})
    env.beat(1)
    write_request(env.cfg, {"op": "deploy"}, "01HZZ0000000000000000000C1.req")
    write_request(env.cfg, {"op": "deploy"}, "01HZZ0000000000000000000C2.req")
    r = fresh(env).cycle()
    assert [res for _n, res in r["requests"]] == ["ok", "merged"]
    assert len(env.runner.cmds("ax-deploy.sh")) == 1


def test_request_cap_per_cycle(env):
    env.cfg.max_requests_per_cycle = 2
    env.beat(1)
    for i in range(5):
        write_request(env.cfg, "garbage", f"01HZZ000000000000000000{i:03d}.req")
    r = fresh(env).cycle()
    assert len(r["requests"]) == 2
    assert len(list(env.cfg.requests_dir.glob("*.req"))) == 3


# --- 索引の再作成 ---
def test_index_rebuild_runs_first_then_hourly(env):
    env.beat(1)
    assert fresh(env).cycle()["index"] == "ok"
    env.clock.t += 600
    assert fresh(env).cycle()["index"] is None
    env.clock.t += 3600
    assert fresh(env).cycle()["index"] == "ok"
    assert len(env.runner.cmds("ax-redact-index.sh")) == 2


def test_index_failure_is_logged_and_retried_next_cycle(env):
    env.runner.on("ax-redact-index.sh", Result(1))
    env.beat(1)
    assert fresh(env).cycle()["index"] == "failed"
    assert fresh(env).cycle()["index"] == "failed"  # マーカーが無いので次の周期でやり直す
    assert [e["data"]["result"] for e in log_events(env.cfg) if e["data"].get("op") == "redact-index"] == ["failed:1", "failed:1"]


# --- 周期の排他 ---
def test_overlapping_cycle_is_skipped_by_flock(env):
    import fcntl

    d = env.cfg.state_dir / "locks"
    d.mkdir(exist_ok=True)
    f = open(d / "hostwatch.lock", "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert run_cycle(env.cfg, env.runner, quiet_log(env.cfg), clock=env.clock) is None
        assert env.runner.calls == []
    finally:
        f.close()
    assert run_cycle(env.cfg, env.runner, quiet_log(env.cfg), clock=env.clock) is not None


# --- ログ ---
def test_log_lines_are_valid_events_with_ops_targets_results_only(env):
    from axcommon import schemas

    register(env.cfg, "timeline")
    set_ps(env, {"timeline": ("exited", "")})
    env.beat(1)
    fresh(env).cycle()
    evs = log_events(env.cfg)
    assert evs and all(schemas.errors("event", e) == [] for e in evs)
    assert all(e["source"]["component"] == "hostwatch" and e["source"]["host"] == "test-host" for e in evs)
    act = [e for e in evs if e["data"].get("op") == "up"]
    assert act and set(act[0]["data"]) <= {"op", "target", "result", "reason"}
    assert oct(os.stat(env.cfg.log_path).st_mode & 0o777) == "0o600"


# --- 構成の検査: コンテナに Docker のソケットも sudo も渡さない。ax-hostctl と docker グループの前提がない ---
def test_no_container_gets_docker_socket_or_sudo():
    for f in sorted((ROOT / "compose.d").glob("*.yaml")) + [ROOT / "compose.yaml", ROOT / "docker" / "base.yaml"]:
        text = f.read_text()
        assert "docker.sock" not in text, f
        assert not re.search(r"^\s*privileged:\s*true", text, re.M), f
        assert "sudo" not in re.sub(r"#.*", "", text), f


def test_no_ax_hostctl_or_docker_group_assumption_in_code_or_config():
    bad = re.compile(r"ax-hostctl|usermod\s+-a?G\s+docker|gpasswd\s+-a\s+\S+\s+docker|\bgroups?:\s*\[?\s*docker\b")
    hits = []
    for base in ("core", "axcommon", "host", "compose.d", "config", "docker", "tools", "plugins", "runtime", "templates"):
        for p in (ROOT / base).rglob("*"):
            if p.is_file() and p.suffix in (".py", ".sh", ".yaml", ".yml", ".service", ".timer", ".tmpl", "") and "__pycache__" not in p.parts:
                for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                    if bad.search(line) and not line.lstrip().startswith(("#", "//", '"""')) and "使わない" not in line and "ない" not in line:
                        hits.append(f"{p.relative_to(ROOT)}:{i}")
    assert hits == []
    for path in sorted((ROOT / "compose.d").glob("*.yaml")) + [ROOT / "compose.yaml", ROOT / "docker" / "base.yaml"]:
        doc = yaml.safe_load(path.read_text()) or {}
        for service in (doc.get("services") or {}).values():
            assert "docker" not in (service.get("group_add") or []), path


@pytest.mark.parametrize("output", ["not json", "null", "[1]", '{"Service":"ledger"}', '{"Service":"ledger","State":"running"}\nbroken'])
def test_unreadable_ps_defers_restart_requests_and_keeps_index_work(env, output):
    register(env.cfg, "ledger")
    env.runner.on("ps --all", Result(0, output))
    p = write_request(env.cfg, {"op": "restart", "name": "ledger"})
    result = fresh(env).cycle()
    assert result["started"] == [] and result["requests"] == [(p.name, "deferred")]
    assert p.exists() and not env.runner.cmds(" up ") and not env.runner.cmds(" restart ")
    assert result["index"] == "ok"


def test_failed_ps_does_not_consume_a_restart_request(env):
    register(env.cfg, "ledger")
    env.runner.on("ps --all", Result(1))
    p = write_request(env.cfg, {"op": "restart", "name": "ledger"})
    assert fresh(env).cycle()["requests"] == [(p.name, "deferred")]
    assert p.exists()
    set_ps(env, {"ledger": ("running", "healthy")})
    assert fresh(env).cycle()["requests"] == [(p.name, "ok")]
    assert env.runner.cmds(" restart ledger")


def test_paseo_is_excluded_even_when_not_the_configured_commander(env):
    env.runner.on("config --services", Result(0, services_out("paseo", "paseo-other", "ledger")))
    register(env.cfg, "paseo", "paseo-other", "ledger")
    set_ps(env, {"ledger": ("running", "healthy")})
    fresh(env).cycle()
    assert not env.runner.cmds(" up ") and not env.runner.cmds(" restart ")


def test_explicit_null_name_is_rejected_for_nameless_operations(env):
    p = write_request(env.cfg, {"op": "deploy", "name": None})
    assert fresh(env).cycle()["requests"] == [(p.name, "rejected")]
    assert not env.runner.cmds("ax-deploy.sh")


def test_request_scripts_inherit_the_configured_privilege_runner(env):
    env.cfg.sudo = ("fake-sudo", "-n")
    write_request(env.cfg, {"op": "deploy"})
    fresh(env).cycle()
    i = next(i for i, c in enumerate(env.runner.calls) if "ax-deploy.sh" in " ".join(c))
    assert env.runner.envs[i]["AX_HOSTWATCH_SUDO"] == "fake-sudo -n"
    assert env.runner.envs[i]["AX_UP_SUDO"] == "fake-sudo -n"


def test_existing_host_log_permissions_are_restricted(env):
    env.cfg.log_path.write_text("")
    env.cfg.log_path.chmod(0o644)
    quiet_log(env.cfg).event("host.action", "sudo", {"op": "check", "result": "ok"})
    assert env.cfg.log_path.stat().st_mode & 0o777 == 0o600

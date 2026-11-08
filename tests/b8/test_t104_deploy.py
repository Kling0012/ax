"""T-104 の deploy 部分（S-16.7、F-18.3 の 4、D-10.3）: host/ax-deploy.sh の本体を偽の実行器で動かす。

実機（本物の git と docker compose build、healthz による登録、中核が通らないときの前のタグでの起動し直し）は未実行。
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.b8.hostwatch_helpers import ROOT, Result, log_events, make_cfg, quiet_log, register
from tests.fakes.b8.fake_runner import FakeRunner

from hostwatch.deploy import (EXIT_BUSY, EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, EXIT_REJECTED, EXIT_ROLLED_BACK, Deploy, accepted_tag_for,
                              changed_services, code_changed)

OLD = "a" * 40
NEW = "b1c2d3e4f5a6" + "7" * 28


def conf(tag, services):
    definitions = {s: {"image": f"ax/core:{tag}", "command": c, "restart": r} for s, (c, r) in services.items()}
    definitions["img-runtime"] = {"image": f"ax/runtime:{tag}", "build": {"context": "."}, "profiles": ["build"]}
    return json.dumps({"services": definitions})


BASE = {"timeline": (["python", "-m", "core.timeline"], "unless-stopped"),
        "ledger": (["python", "-m", "core.ledger"], "unless-stopped"),
        "observer": (["python", "-m", "core.observer"], "unless-stopped"),
        "sl-a": (["python", "-m", "plugins.a"], "unless-stopped"),
        "archive-init": (["sh", "-c", "mkdir"], "no")}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg.repo = ROOT
    (cfg.ax_home / "uv.lock").write_text('[[package]]\nname = "jsonschema"\nversion = "4.26.0"\n')
    monkeypatch.delenv("AX_JSONSCHEMA_VERSION", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    r = FakeRunner()
    r.on("rev-parse HEAD", Result(0, OLD + "\n"))
    r.on("rev-parse FETCH_HEAD", Result(0, NEW + "\n"))
    r.on("tag --points-at", Result(0, f"accepted/{NEW[:12]}\n"))
    r.on("diff --name-only", Result(0, "core/ledger/service.py\n"))
    seq = {"n": 0}

    def config(argv):
        seq["n"] += 1
        return Result(0, conf("oldtag000000" if seq["n"] == 1 else NEW[:12], BASE))

    r.on("config --format json", config)

    class E:
        pass

    e = E()
    e.cfg, e.r, e.tmp = cfg, r, tmp_path
    e.deploy = lambda: Deploy(cfg, r, quiet_log(cfg))
    e.events = lambda: log_events(cfg)
    e.kinds = lambda: [x["kind"] for x in log_events(cfg)]
    return e


def current(e):
    return (e.cfg.state_dir / "deploy" / "current.env").read_text().strip()


def previous(e):
    p = e.cfg.state_dir / "deploy" / "previous.env"
    return p.read_text().strip() if p.exists() else None


# --- 印のない main は rejected で何も変えない ---
def test_main_without_accepted_tag_is_rejected_and_nothing_changes(env):
    env.r.on("tag --points-at", Result(0, "v1.0\n"))
    assert env.deploy().execute() == EXIT_REJECTED
    assert not env.r.cmds(" merge ") and not env.r.cmds(" build") and not env.r.cmds("ax-up.sh")
    assert current(env) == "AX_TAG=oldtag000000" and previous(env) is None
    assert "deploy.rejected" in env.kinds()


def test_tag_for_another_commit_does_not_count(env):
    env.r.on("tag --points-at", Result(0, "accepted/deadbeefcafe\n"))
    assert env.deploy().execute() == EXIT_REJECTED


def test_tag_check_comes_before_merge(env):
    env.r.on("tag --points-at", Result(0, ""))
    env.deploy().execute()
    assert env.r.cmds(" fetch ") and not env.r.cmds("merge")


def test_accepted_tag_for_matches_by_prefix_length_7_to_40():
    assert accepted_tag_for(["accepted/b1c2d3e"], NEW) == "accepted/b1c2d3e"
    assert accepted_tag_for(["accepted/b1c2d3"], NEW) is None  # 6 桁は短い
    assert accepted_tag_for(["accepted/" + NEW], NEW)
    assert accepted_tag_for(["accepted/zzzzzzzz", "x"], NEW) is None


@pytest.mark.parametrize("configured,lock,expected", [
    (None, '[[package]]\nname = "jsonschema"\nversion = "4.26.0"\n', "4.26.0"),
    ("4.20.0", "invalid lock", "4.20.0"),
])
def test_build_jsonschema_version(env, monkeypatch, configured, lock, expected):
    (env.cfg.ax_home / "uv.lock").write_text(lock)
    if configured is not None:
        monkeypatch.setenv("AX_JSONSCHEMA_VERSION", configured)
    assert env.deploy().execute() == EXIT_OK
    build = env.r.cmds(" build")[0]
    assert f"--build-arg JSONSCHEMA_VERSION={expected}" in build


def test_build_jsonschema_version_from_compose_environment(env):
    env.r.on("config --environment", Result(0, "AX_JSONSCHEMA_VERSION=4.21.0\n"))
    assert env.deploy().execute() == EXIT_OK
    assert "--build-arg JSONSCHEMA_VERSION=4.21.0" in env.r.cmds(" build")[0]


@pytest.mark.parametrize("lock", ["invalid lock", '[[package]]\nname = "other"\nversion = "1.0"\n',
                                  '[[package]]\nname = "jsonschema"\nversion = "bad"\n',
                                  '[[package]]\nname = "jsonschema"\nversion = "4.26.0bad"\n'])
def test_invalid_jsonschema_lock_fails_before_build(env, lock):
    (env.cfg.ax_home / "uv.lock").write_text(lock)
    assert env.deploy().execute() == EXIT_FAILED
    assert not env.r.cmds(" build") and not env.r.cmds("ax-up.sh")
    assert current(env) == "AX_TAG=oldtag000000"
    assert any("uv.lock jsonschema version" in str(event) for event in env.events())


# --- 正常 ---
def test_happy_path_commands_and_files(env):
    register(env.cfg, "timeline", "ledger", "observer", "sl-a")
    assert env.deploy().execute() == EXIT_OK
    cmds = [" ".join(c) for c in env.r.calls]
    order = [next(i for i, c in enumerate(cmds) if k in c) for k in
             ("rev-parse HEAD", "fetch local main refs/tags/accepted/*:refs/tags/accepted/*", "rev-parse FETCH_HEAD", "tag --points-at",
              "merge --ff-only FETCH_HEAD", " build", "diff --name-only", "ax-up.sh")]
    assert order == sorted(order)
    assert all(c.startswith(f"git -C {env.cfg.ax_home} ") for c in cmds if c.startswith("git"))
    build = env.r.cmds(" build")[0]
    assert "sudo -n docker compose --profile build -p ax " in build
    assert current(env) == f"AX_TAG={NEW[:12]}" and previous(env) == "AX_TAG=oldtag000000"
    assert oct(os.stat(env.cfg.state_dir / "deploy" / "current.env").st_mode & 0o777) == "0o600"
    # 変わった物（ledger はコードが変わった）だけを 1 つずつ。一度きりの archive-init は除く
    ups = [c for c in env.r.cmds("ax-up.sh")]
    assert [u.split()[-1] for u in ups] == ["ledger"]
    assert f"--profile {env.cfg.profile_path}" in ups[0]
    idx = [" ".join(c) for c in env.r.calls].index(ups[0])
    assert env.r.envs[idx]["AX_HOME"] == str(env.cfg.ax_home)
    assert "deploy.completed" in env.kinds()


def test_every_service_is_started_one_by_one_in_monitoring_first_order(env):
    register(env.cfg, "timeline", "ledger", "observer", "sl-a")
    env.r.on("diff --name-only", Result(0, "axcommon/ids.py\n"))  # 共通部品の変更: ax/ のイメージ全部
    assert env.deploy().execute() == EXIT_OK
    assert [u.split()[-1] for u in env.r.cmds("ax-up.sh")] == ["observer", "ledger", "timeline", "sl-a"]


def test_unregistered_changed_service_is_not_started_but_new_one_is(env):
    register(env.cfg, "timeline")
    n = {"i": 0}

    def config(argv):
        n["i"] += 1
        svcs = dict(BASE)
        if n["i"] > 1:
            svcs["sl-new"] = (["python", "-m", "plugins.new"], "unless-stopped")
        return Result(0, conf("oldtag000000" if n["i"] == 1 else NEW[:12], svcs))

    env.r.on("config --format json", config)
    env.r.on("diff --name-only", Result(0, "axcommon/ids.py\nplugins/new/x.py\n"))
    assert env.deploy().execute() == EXIT_OK
    assert [u.split()[-1] for u in env.r.cmds("ax-up.sh")] == ["timeline", "sl-new"]  # ledger・observer は未登録なので起動しない


def test_no_change_when_fetch_head_equals_head(env):
    env.r.on("rev-parse FETCH_HEAD", Result(0, OLD + "\n"))
    env.r.on("tag --points-at", Result(0, "accepted/" + OLD[:12]))
    (env.cfg.state_dir / "deploy/current.env").write_text(f"AX_TAG={OLD[:12]}\n")
    env.r.on("config --format json", Result(0, json.dumps({"services": {"img-runtime": {"image": f"ax/runtime:{OLD[:12]}", "build": {"context": "."}}}})))
    assert env.deploy().execute() == EXIT_OK
    assert not env.r.cmds("merge") and not any("build" in c and "--build-arg" in c for c in env.r.calls) and current(env) == f"AX_TAG={OLD[:12]}"


def test_definition_change_without_code_change_restarts_the_service(env):
    register(env.cfg, "timeline", "ledger")
    n = {"i": 0}

    def config(argv):
        n["i"] += 1
        svcs = {k: dict(v) if False else v for k, v in BASE.items()}
        if n["i"] > 1:
            svcs = dict(BASE)
            svcs["timeline"] = (["python", "-m", "core.timeline", "--new-flag"], "unless-stopped")
        return Result(0, conf("oldtag000000" if n["i"] == 1 else NEW[:12], svcs))

    env.r.on("config --format json", config)
    env.r.on("diff --name-only", Result(0, "compose.d/b1.yaml\n"))
    env.deploy().execute()
    assert [u.split()[-1] for u in env.r.cmds("ax-up.sh")] == ["timeline"]


# --- 失敗 ---
def test_fetch_failure_changes_nothing(env):
    env.r.on(" fetch ", Result(128))
    assert env.deploy().execute() == EXIT_FAILED
    assert not env.r.cmds("merge") and current(env) == "AX_TAG=oldtag000000"


def test_merge_failure_stops_before_tag_file_changes(env):
    env.r.on("merge --ff-only", Result(1))
    assert env.deploy().execute() == EXIT_FAILED
    assert current(env) == "AX_TAG=oldtag000000" and not env.r.cmds(" build")


def test_build_failure_restores_previous_env_and_starts_nothing(env):
    register(env.cfg, "ledger")
    env.r.on(" build", Result(1))
    assert env.deploy().execute() == EXIT_FAILED
    assert current(env) == "AX_TAG=oldtag000000"
    assert not env.r.cmds("ax-up.sh") and "deploy.build_failed" in env.kinds()


def test_core_service_not_healthy_rolls_back_to_previous_tag(env):
    register(env.cfg, "timeline", "ledger", "observer")
    env.r.on("diff --name-only", Result(0, "axcommon/ids.py\n"))
    seen = []

    def up(argv):
        svc = argv[-1]
        seen.append((svc, (env.cfg.state_dir / "deploy" / "current.env").read_text().strip()))
        # ledger の最初の起動（新しいタグ）だけ失敗する
        return Result(1) if svc == "ledger" and len(seen) == 2 else Result(0)

    env.r.on("ax-up.sh", up)
    assert env.deploy().execute() == EXIT_ROLLED_BACK
    assert seen == [("observer", f"AX_TAG={NEW[:12]}"), ("ledger", f"AX_TAG={NEW[:12]}"), ("ledger", "AX_TAG=oldtag000000")]
    assert current(env) == "AX_TAG=oldtag000000"
    rb = [e for e in env.events() if e["kind"] == "deploy.rolled_back"]
    assert len(rb) == 1 and rb[0]["data"]["from_tag"] == NEW[:12] and rb[0]["data"]["to_tag"] == "oldtag000000" and rb[0]["data"]["service"] == "ledger"
    assert not any(s == "timeline" for s, _ in seen)  # 中核が通らなければ、残りは起動しない


def test_subloop_failure_skips_only_that_service_and_continues(env):
    register(env.cfg, "timeline", "sl-a")
    env.r.on("diff --name-only", Result(0, "axcommon/ids.py\n"))
    env.r.on("ax-up.sh", lambda argv: Result(1) if argv[-1] == "sl-a" else Result(0))
    assert env.deploy().execute() == EXIT_PARTIAL
    assert [u.split()[-1] for u in env.r.cmds("ax-up.sh")] == ["timeline", "sl-a"]
    assert "deploy.rolled_back" not in env.kinds()
    assert current(env) == f"AX_TAG={NEW[:12]}"  # 新しいタグのまま
    done = [e for e in env.events() if e["kind"] == "deploy.completed"][0]
    assert done["data"]["failed"] == "sl-a" and done["data"]["started"] == "timeline"


def test_subloop_service_not_registered_when_up_fails(env):
    """登録は ax-up.sh の中（healthz の後）。ax-up.sh が失敗した物は登録されていない（deploy は登録を作らない）。"""
    env.r.on("ax-up.sh", Result(1))
    register(env.cfg, "sl-a")
    env.r.on("diff --name-only", Result(0, "plugins/a/x.py\n"))
    env.deploy().execute()
    assert list(env.cfg.registered_dir.iterdir()) == [env.cfg.registered_dir / "sl-a"]  # 既存の印のまま。新しい印を作っていない


def test_concurrent_deploys_are_refused_with_busy(env):
    import fcntl

    (env.cfg.state_dir / "locks").mkdir(exist_ok=True)
    f = open(env.cfg.state_dir / "locks" / "deploy.lock", "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert env.deploy().execute() == EXIT_BUSY
        assert env.r.calls == []
    finally:
        f.close()


# --- 作業用イメージの claude ---
def test_vendor_claude_is_copied_before_build(env):
    ver = env.cfg.ax_home / "runtime" / "images" / "agent-cli"
    ver.mkdir(parents=True)
    (ver / "CLAUDE_VERSION").write_text("9.9.9\n")
    src = env.tmp / "home" / ".local" / "share" / "mise" / "installs" / "claude" / "9.9.9"
    src.mkdir(parents=True)
    (src / "claude").write_text("#!/bin/sh\n")
    env.deploy().execute()
    assert (ver / "vendor" / "claude").read_text() == "#!/bin/sh\n"
    assert env.r.cmds(" build")


def test_vendor_claude_missing_rejects_deploy_before_build(env):
    ver = env.cfg.ax_home / "runtime" / "images" / "agent-cli"
    ver.mkdir(parents=True)
    (ver / "CLAUDE_VERSION").write_text("9.9.9\n")
    assert env.deploy().execute() == EXIT_REJECTED
    assert "deploy.vendor_missing" in env.kinds() and not env.r.cmds(" build")
    assert current(env) == "AX_TAG=oldtag000000"


# --- 純粋な判定 ---
def test_changed_services_pure():
    old = json.loads(conf("t0", BASE))["services"]
    new = json.loads(conf("t1", BASE))["services"]
    order, fresh = changed_services(old, new, "t0", "t1", [], {"timeline", "ledger"})
    assert order == [] and fresh == []  # タグだけが違う: 変わっていない
    order, fresh = changed_services(old, new, "t0", "t1", ["core/ledger/a.py"], {"timeline", "ledger"})
    assert order == ["ledger"]
    new2 = dict(new)
    new2["sl-b"] = {"image": "ax/runtime:t1", "command": ["x"], "restart": "unless-stopped"}
    order, fresh = changed_services(old, new2, "t0", "t1", [], set())
    assert order == ["sl-b"] and fresh == ["sl-b"]


@pytest.mark.parametrize("svc,defn,paths,expect", [
    ("timeline", {"image": "ax/core:x", "command": ["python", "-m", "core.timeline"]}, ["core/timeline/api.py"], True),
    ("timeline", {"image": "ax/core:x", "command": ["python", "-m", "core.timeline"]}, ["core/ledger/api.py"], False),
    ("memory-connector", {"image": "ax/core:x", "command": ["python", "/ax/core/memory-connector/main.py"]}, ["core/memory-connector/main.py"], True),
    ("egress-proxy", {"image": "ax/core:x", "command": ["python", "/ax/core/egress-proxy/egress_proxy.py"]}, ["core/egress-proxy/egress_proxy.py"], True),
    ("sl-a", {"image": "ax/runtime:x", "command": ["x"]}, ["plugins/a/node.py"], True),
    ("sl-a", {"image": "ax/runtime:x", "command": ["x"]}, ["plugins/b/node.py"], False),
    ("sl-a", {"image": "ax/runtime:x", "command": ["x"]}, ["runtime/runner.py"], True),
    ("sl-a", {"image": "ax/runtime:x", "command": ["x"]}, ["runtime/images/verify/node/Dockerfile"], False),
    ("timeline", {"image": "ax/core:x", "command": ["x"]}, ["schemas/event.schema.json"], True),
    ("timeline", {"image": "postgres:16", "command": ["x"]}, ["schemas/event.schema.json"], False),
    ("timeline", {"image": "ax/core:x", "command": ["x"]}, ["docs/a.md"], False),
])
def test_code_changed(svc, defn, paths, expect):
    assert code_changed(svc, defn, paths) is expect


# --- 呼び出し口 ---
def test_ax_deploy_sh_is_a_thin_wrapper_and_syntax_ok():
    p = ROOT / "host" / "ax-deploy.sh"
    assert subprocess.run(["bash", "-n", str(p)]).returncode == 0
    assert "python3 -P -m hostwatch deploy" in p.read_text()
    assert os.access(p, os.X_OK)


@pytest.mark.parametrize("output,rc", [("", 1), ("not json", 0), ("{}", 0), ('{"services":[]}', 0)])
def test_unavailable_old_configuration_never_starts_unregistered_services(env, output, rc):
    env.r.on("config --format json", Result(rc, output))
    assert env.deploy().execute() == EXIT_FAILED
    assert not env.r.cmds(" merge ") and not env.r.cmds(" build") and not env.r.cmds("ax-up.sh")
    assert current(env) == "AX_TAG=oldtag000000"


def test_unavailable_new_configuration_restores_tag_and_starts_nothing(env):
    n = {"i": 0}
    def config(argv):
        n["i"] += 1
        return Result(0, conf("oldtag000000", BASE)) if n["i"] == 1 else Result(1)
    env.r.on("config --format json", config)
    assert env.deploy().execute() == EXIT_FAILED
    assert current(env) == "AX_TAG=oldtag000000" and not env.r.cmds("ax-up.sh")


def test_failed_build_is_retried_with_new_service_still_in_the_plan(env):
    register(env.cfg, "ledger")
    state = {"head": OLD, "fail": True}
    env.r.on("rev-parse HEAD", lambda argv: Result(0, state["head"]))
    def merge(argv):
        state["head"] = NEW
        return Result(0)
    def config(argv):
        svcs = dict(BASE)
        if state["head"] == NEW:
            svcs["sl-new"] = (["python", "-m", "runtime.subloop"], "unless-stopped")
        return Result(0, conf("oldtag000000" if state["head"] == OLD else NEW[:12], svcs))
    env.r.on("merge --ff-only", merge)
    env.r.on("config --format json", config)
    env.r.on(lambda argv: "--build-arg" in argv, lambda argv: Result(1 if state["fail"] else 0))
    assert env.deploy().execute() == EXIT_FAILED
    assert current(env) == "AX_TAG=oldtag000000" and not env.r.cmds("ax-up.sh")
    state["fail"] = False
    assert env.deploy().execute() == EXIT_OK
    assert [c.split()[-1] for c in env.r.cmds("ax-up.sh")] == ["ledger", "sl-new"]
    assert previous(env) == "AX_TAG=oldtag000000"
    assert json.loads((env.cfg.state_dir / "deploy/pending.json").read_text())["status"] == "completed"
    count = sum("--build-arg" in c for c in env.r.calls)
    assert env.deploy().execute() == EXIT_OK
    assert sum("--build-arg" in c for c in env.r.calls) == count


def test_partial_subloop_deploy_is_retried_instead_of_reported_complete(env):
    register(env.cfg, "sl-a")
    env.r.on("diff --name-only", Result(0, "plugins/subloop/a/node.py\n"))
    env.r.on("ax-up.sh", Result(1))
    assert env.deploy().execute() == EXIT_PARTIAL
    env.r.on("rev-parse HEAD", Result(0, NEW))
    env.r.on("ax-up.sh", Result(0))
    assert env.deploy().execute() == EXIT_OK
    assert len(env.r.cmds("ax-up.sh")) == 2
    assert previous(env) == "AX_TAG=oldtag000000"


def test_diff_failure_never_suppresses_a_required_restart(env):
    register(env.cfg, "ledger")
    env.r.on("diff --name-only", Result(1))
    assert env.deploy().execute() == EXIT_FAILED
    assert not env.r.cmds("ax-up.sh") and current(env) == "AX_TAG=oldtag000000"


@pytest.mark.parametrize("svc,path", [
    ("sl-work-tdd", "plugins/subloop/work-tdd/main.py"),
    ("worker-pool", "plugins/resource/worker-pool/app.py"),
    ("timer-runner", "plugins/timer/hibi-state/run.py"),
    ("timer-runner", "runtime/timer_runner/engine.py"),
    ("sl-work-tdd", "plugins/nodes/git/sync.py"),
])
def test_code_changes_follow_the_actual_package_layout(svc, path):
    assert code_changed(svc, {"image": "ax/runtime:tag"}, [path])


def test_deploy_never_starts_paseo_even_as_a_new_service(env):
    n = {"i": 0}
    def config(argv):
        n["i"] += 1
        svcs = dict(BASE)
        if n["i"] > 1:
            svcs["paseo-other"] = (["x"], "unless-stopped")
        return Result(0, conf("oldtag000000" if n["i"] == 1 else NEW[:12], svcs))
    env.r.on("config --format json", config)
    assert env.deploy().execute() == EXIT_OK
    assert not env.r.cmds("ax-up.sh")


def test_vendor_is_refreshed_even_if_an_old_binary_exists(env):
    ver = env.cfg.ax_home / "runtime/images/agent-cli"
    (ver / "vendor").mkdir(parents=True)
    (ver / "vendor/claude").write_text("old")
    (ver / "CLAUDE_VERSION").write_text("9.9.9")
    src = env.tmp / "home/.local/share/mise/installs/claude/9.9.9"
    src.mkdir(parents=True)
    (src / "claude").write_text("new")
    assert env.deploy().execute() == EXIT_OK
    assert (ver / "vendor/claude").read_text() == "new"


def test_new_build_only_services_are_never_started():
    new = {"img-agent-cli": {"profiles": ["build"], "image": "ax/agent-cli:new"},
           "img-runtime": {"profiles": ["build"], "image": "ax/runtime:new"},
           "sl-new": {"image": "ax/runtime:new"}}
    assert changed_services({}, new, "old", "new", [], set()) == (["sl-new"], ["sl-new"])


def test_deploy_network_check_failure_preserves_current_env(env):
    current_file = env.cfg.state_dir / "deploy" / "current.env"
    current_file.write_text("AX_TAG=oldtag000000\nAX_CORE_SUBNET=10.41.0.0/24\n")
    before = current_file.read_bytes()
    env.r.on("ax-ipam-check", Result(1, "", "network mismatch"))
    assert env.deploy().execute() == EXIT_FAILED
    assert current_file.read_bytes() == before
    assert not env.r.cmds(" build") and not env.r.cmds("ax-up.sh")


def test_deploy_preserves_other_current_env_values(env, monkeypatch):
    monkeypatch.setenv("AX_JSONSCHEMA_VERSION", "4.26.0")
    current_file = env.cfg.state_dir / "deploy" / "current.env"
    current_file.write_text("AX_TAG=oldtag000000\nAX_CORE_SUBNET=10.41.0.0/24\n")
    assert env.deploy().execute() == EXIT_OK
    assert current_file.read_text() == f"AX_TAG={NEW[:12]}\nAX_CORE_SUBNET=10.41.0.0/24\n"

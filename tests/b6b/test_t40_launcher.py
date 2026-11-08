"""T-40 規則と隔離（launcher 側）。F-18.2 の docker run の引数、呼び出し元が指定できない物、拒否、起動後の隔離の確認。

偽の docker・偽の sudo で動かす（実際の docker と sudo は使わない）。実環境でしか通せない部分（docker inspect の実物、ax-job から外へ届かないこと、
許可先にないホストへの CONNECT が 403 であること、launcher が使えない間 parked になること）は status/b6b.md の表に残す。
"""
import json
import os

import pytest

from tests.b6b.helpers import TOKEN, flag_values


def test_t40_docker_run_args_are_fixed(sb):
    code, out = sb.start("j1")
    assert code == 0, out
    assert out == {"job_id": "j1", "root": "j1", "wd": "j1"}
    a = sb.run_args("j1")
    assert a[:2] == ["run", "-d"]
    assert flag_values(a, "--name") == ["ax-job-j1"]
    labels = flag_values(a, "--label")
    assert labels == ["ax.managed=1", "ax.job=j1", "ax.chain=j1", "ax.wd=j1"]
    assert flag_values(a, "--user") == [f"{os.getuid()}:{os.getgid()}"]
    assert "--read-only" in a
    assert flag_values(a, "--tmpfs") == ["/tmp:rw,size=512m"]
    assert flag_values(a, "--cap-drop") == ["ALL"]
    assert flag_values(a, "--security-opt") == ["no-new-privileges"]
    assert flag_values(a, "--pids-limit") == ["512"]
    assert flag_values(a, "--memory") == ["4g"]
    assert flag_values(a, "--cpus") == ["2"]
    assert flag_values(a, "--network") == ["ax-job"]
    assert flag_values(a, "-w") == ["/work"]
    vs = flag_values(a, "-v")
    assert vs == [f"{sb.work_dir('j1')}:/work:rw", f"{sb.chain_dir('j1')}:/tmp-hdd:rw"]
    env = flag_values(a, "-e")
    assert "TMPDIR=/tmp-hdd" in env and "HOME=/tmp-hdd/home" in env
    assert "HTTP_PROXY=http://job-j1@egress-proxy:3128" in env and "HTTPS_PROXY=http://job-j1@egress-proxy:3128" in env
    assert "NO_PROXY=egress-proxy" in env
    assert "ANTHROPIC_BASE_URL=http://egress-proxy:3425" in env
    for bad in ("--privileged", "--cap-add", "--pid", "--ipc", "--device", "--volumes-from"):
        assert bad not in a
    assert not any("docker.sock" in x for x in a)
    assert not any(str(sb.secrets) in x or str(sb.state) in x for x in a)


def test_t40_token_is_inherited_not_on_command_line(sb):
    code, _ = sb.start("j1")
    assert code == 0
    a = sb.run_args("j1")
    assert "ANTHROPIC_AUTH_TOKEN" in flag_values(a, "-e")  # 値なしの -e（環境から継承）
    assert not any(TOKEN in x for x in a)
    calls = [c for c in sb.fd.calls() if c["argv"][0] == "run"]
    assert calls and calls[0]["has_token"] is True and calls[0]["token_len"] == len(TOKEN)
    log = sb.sudo_log.read_text()
    assert "--preserve-env=ANTHROPIC_AUTH_TOKEN" in log
    assert TOKEN not in log
    for f in sb.state.rglob("*"):
        if f.is_file():
            assert TOKEN not in f.read_text(errors="ignore"), f


def test_t40_inspect_isolation_matches(sb):
    code, _ = sb.start("j1")
    assert code == 0
    info = sb.fd.container("ax-job-j1")
    assert info["HostConfig"]["Privileged"] is False
    assert not info["HostConfig"]["CapAdd"]
    assert info["HostConfig"]["ReadonlyRootfs"] is True
    assert info["HostConfig"]["NetworkMode"] == "ax-job"
    assert {m["Destination"] for m in info["Mounts"]} == {"/work", "/tmp-hdd"}
    assert not any("docker.sock" in m["Source"] for m in info["Mounts"])


@pytest.mark.parametrize("field", ["image", "mounts", "network", "command", "host_path", "container_path", "privileged", "volumes", "user", "cap_add", "entrypoint"])
def test_t40_caller_cannot_specify_image_mounts_network_command(sb, field):
    code, out = sb.start("j1", **{field: "x"})
    assert code == 2 and out["error"]["reason"] == "unknown_field"
    assert sb.run_calls() == []


def test_t40_unknown_and_foreign_subcommands_rejected(sb):
    cmds = ["", "job-fly j1", "ls", "docker run --privileged x", "job-status j1; id", "job-status ../x", "job-status A", "job-status j1 j2",
            "job-list extra", "job-start", "job-status", "submit j1", "probe", "job-status j1$(id)", "job-status j1|cat"]
    for cmd in cmds:
        parts = cmd.split(" ", 1)
        code, out = sb.call(parts[0], *(parts[1:] if len(parts) > 1 else []), stdin={})
        assert code == 2, (cmd, code, out)
        assert out["ok"] is False and out["error"]["class"] == "rejected"
    assert sb.fd.calls() == []


def test_t40_env_caller_cannot_use_job_subcommands_and_launcher_cannot_use_env_ones(sb):
    code, out = sb.call("job-list", caller="env")
    assert code == 2 and out["error"]["reason"] == "caller_not_allowed"
    code, out = sb.call("probe", caller="env")
    assert code == 0 and out["ok"] is True and set(out["checks"]) == {"transfer", "commands", "disk", "sudo"}
    code, out = sb.call("probe", caller="launcher")
    assert code == 2 and out["error"]["reason"] == "caller_not_allowed"
    code, out = sb.call("job-list", caller="nobody")
    assert code == 2 and out["error"]["reason"] == "unknown_caller"
    assert sb.fd.calls() == []


def test_t40_forced_command_ignores_extra_argv_and_missing_caller_is_env(sb):
    r = sb.subprocess_env_run("job-list", "--caller", "launcher", "job-start", "x")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["reason"] == "bad_args"
    r = sb.subprocess_env_run("job-list")  # --caller 省略は env と同じ扱い
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["reason"] == "caller_not_allowed"
    r = sb.subprocess_env_run(None, "--caller", "launcher")  # 原コマンドも引数も無い
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["reason"] == "no_subcommand"
    r = sb.subprocess_env_run("job-list", "--caller", "launcher", "--caller", "env")
    assert r.returncode == 2
    assert sb.fd.calls() == []


def test_t40_subprocess_job_start_through_wrapper(sb):
    req = json.dumps({"job_id": "j9", "runner": "agent-cli", "model": "m/x", "allow_hosts": [], "budget_min": 5, "prompt": "test prompt"})
    r = sb.subprocess_env_run("job-start j9", "--caller", "launcher", stdin=req)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout) == {"job_id": "j9", "root": "j9", "wd": "j9"}
    assert len(r.stdout.strip().splitlines()) == 1


@pytest.mark.parametrize("host", ["1.1.1.5", "198.51.100.1", "*", "*.example.com", "example.com:443", "EXAMPLE.com", "a b.com", "[::1]", "::1", "ex..com", ".example.com", "例.jp"])
def test_t40_allow_hosts_must_be_fqdn(sb, host):
    code, out = sb.start("j1", allow_hosts=[host])
    assert code == 2 and out["error"]["reason"] in ("bad_allow_host", "bad_field")
    assert sb.run_calls() == []
    assert not (sb.state / "egress" / "job-j1.allow").exists()


def test_t40_allow_file_written_and_gate_line_only_with_tools(sb):
    code, _ = sb.start("j1", allow_hosts=["api.example.org", "pypi.org", "api.example.org"])
    assert code == 0
    p = sb.state / "egress" / "job-j1.allow"
    assert p.read_text().splitlines() == ["api.example.org", "pypi.org"]
    assert oct(p.stat().st_mode & 0o777) == "0o644"
    code, _ = sb.start("j2", tools=["web-screenshot"], allow_hosts=["example.org"])
    assert code == 0
    assert (sb.state / "egress" / "job-j2.allow").read_text().splitlines() == ["example.org", "@gate 8931"]
    code, out = sb.start("j3", runner="verify-python", model=None, verify="true", tools=["web-gui"])
    assert code == 2  # tools は agent-cli だけ


@pytest.mark.parametrize("brk", ["privileged", "rootfs", "network", "socket", "capadd"])
def test_t40_isolation_not_confirmed_stops_and_reports(sb, brk):
    req = {"job_id": "j1", "runner": "agent-cli", "model": "m/x", "allow_hosts": [], "budget_min": 5}
    code, out = sb.call("job-start", "j1", stdin=req, extra_env={"FAKE_DOCKER_BREAK": brk})
    assert code == 70 and out["error"]["reason"] == "isolation_unconfirmed"
    assert sb.fd.container("ax-job-j1") is None  # 止めて片付けた
    cmds = [c["argv"][0] for c in sb.fd.calls()]
    assert "stop" in cmds and "rm" in cmds


def test_t40_launcher_unavailable_does_not_start(sb):
    (sb.secrets / "model-gateway-key").unlink()  # 試験用の砂場の中のダミー
    code, out = sb.start("j1")
    assert code == 70 and out["error"]["reason"] == "launcher_unavailable"
    assert sb.run_calls() == []
    (sb.secrets / "model-gateway-key").write_text(TOKEN)
    req = {"job_id": "j2", "runner": "agent-cli", "model": "m", "allow_hosts": [], "budget_min": 5}
    code, out = sb.call("job-start", "j2", stdin=req, extra_env={"FAKE_DOCKER_FAIL_RUN": "1"})
    assert code == 70 and out["error"]["reason"] == "docker_run_failed"


def test_t40_runner_lookup_and_required_fields(sb):
    assert sb.start("j1", runner="no-such-runner")[1]["error"]["reason"] == "unknown_runner"
    assert sb.start("j1", model=None)[1]["error"]["reason"] == "missing_field"          # agent-cli は model が要る
    assert sb.start("j1", verify="echo hi")[1]["error"]["reason"] == "bad_field"       # agent-cli は verify を受けない
    code, out = sb.call("job-start", "j1", stdin={"job_id": "other", "runner": "agent-cli", "model": "m", "allow_hosts": [], "budget_min": 1})
    assert code == 2 and out["error"]["reason"] == "job_id_mismatch"
    code, out = sb.call("job-start", "j1", stdin="not json")
    assert code == 2 and out["error"]["reason"] == "bad_json"
    code, out = sb.start("j1", budget_min=0)
    assert code == 2
    code, out = sb.start("j1", env_names=["SECRET_X"])
    assert code == 2 and out["error"]["reason"] == "env_name_not_allowed"
    assert sb.run_calls() == []


def test_t40_runner_env_and_command_are_rendered_per_word(sb):
    code, _ = sb.start("j1", model="zhipu-cp/glm-5.3", session_id="11111111-2222-3333-4444-555555555555")
    assert code == 0
    a = sb.run_args("j1")
    env = flag_values(a, "-e")
    for n in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
        assert f"{n}=zhipu-cp/glm-5.3" in env
    i = a.index("ax/agent-cli:test")
    cmd = a[i + 1:]
    assert cmd[:2] == ["sh", "-c"] and len(cmd) == 3
    assert "claude --model zhipu-cp/glm-5.3 -p --output-format json --dangerously-skip-permissions --session-id 11111111-2222-3333-4444-555555555555" in cmd[2]
    assert "< /work/.ax-prompt/user.md" in cmd[2]
    assert "--fork-session" not in cmd[2]


def test_t40_verify_runner_gets_command_as_single_word_and_no_model_token(sb):
    verify = "cd /work && pnpm test; echo 'x' $(id) && true"
    code, out = sb.start("v1", runner="verify-node", model=None, verify=verify, allow_hosts=["registry.npmjs.org"])
    assert code == 0, out
    a = sb.run_args("v1")
    i = a.index("ax/verify-node:test")
    assert a[i + 1:] == ["sh", "-c", verify]  # 呼び出し元の文字列は 1 語。launcher は shell を通さない
    env = flag_values(a, "-e")
    assert "ANTHROPIC_AUTH_TOKEN" not in env and not any(e.startswith("ANTHROPIC_BASE_URL") for e in env)
    calls = [c for c in sb.fd.calls() if c["argv"][0] == "run"]
    assert calls[0]["has_token"] is False
    assert "--preserve-env" not in sb.sudo_log.read_text()
    code, out = sb.start("v2", runner="verify-node", model=None)  # verify が無い
    assert code == 2 and out["error"]["reason"] == "missing_field"


def test_t40_settings_json_for_agent_cli_only_and_not_overwritten(sb):
    assert sb.start("j1")[0] == 0
    s = sb.chain_dir("j1") / "home" / ".claude" / "settings.json"
    assert json.loads(s.read_text()) == {"promptCacheTtl": "1h", "subagentPromptCacheTtl": "1h"}
    assert oct((sb.chain_dir("j1") / "home").stat().st_mode & 0o777) == "0o700"
    s.write_text('{"custom": 1}')
    sb.fd.finish("ax-job-j1")
    assert sb.call("job-status", "j1")[0] == 0
    assert sb.start("j2", workdir_id="j1")[0] == 0  # 新しい連鎖。別の HOME
    assert s.read_text() == '{"custom": 1}'
    assert sb.start("v1", runner="verify-python", model=None, verify="true")[0] == 0
    assert not (sb.chain_dir("v1") / "home" / ".claude").exists()


def test_t40_status_finalizes_once_saves_log_removes_container_archives_allow(sb):
    assert sb.start("j1")[0] == 0
    code, out = sb.call("job-status", "j1")
    assert code == 0 and out["state"] == "running"
    sb.fd.finish("ax-job-j1", exit_code=3, logs="hello from container\n")
    code, out = sb.call("job-status", "j1")
    assert code == 0 and out["state"] == "exited" and out["exit_code"] == 3 and out["root"] == "j1" and out["wd"] == "j1"
    assert (sb.work_dir("j1") / ".container-j1.log").read_text() == "hello from container\n"
    assert sb.fd.container("ax-job-j1") is None  # docker rm 済み
    assert not (sb.state / "egress" / "job-j1.allow").exists()
    assert list((sb.state / "egress" / "archive").glob("job-j1.allow.*"))  # 削除せず移す
    code2, out2 = sb.call("job-status", "j1")
    assert code2 == 0 and out2 == out  # 冪等
    assert sb.call("job-status", "nosuch")[0] == 3


def test_t40_usage_file_from_transcript(sb):
    sid = "11111111-2222-3333-4444-555555555555"
    assert sb.start("j1", session_id=sid)[0] == 0
    tr = sb.chain_dir("j1") / "home" / ".claude" / "projects" / "-work"
    tr.mkdir(parents=True)
    lines = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5}}},
        {"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 999, "cache_read_input_tokens": 999, "cache_creation_input_tokens": 999}}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 20, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 7}}},
    ]
    (tr / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    sb.fd.finish("ax-job-j1")
    assert sb.call("job-status", "j1")[0] == 0
    u = json.loads((sb.work_dir("j1") / ".usage-j1.json").read_text())
    assert u == {"first_input": 10, "first_cache_read": 100, "last_context": 327}


def test_t40_usage_missing_items_not_written(sb):
    assert sb.start("j1", session_id="11111111-2222-3333-4444-555555555555")[0] == 0  # 記録が無い
    sb.fd.finish("ax-job-j1")
    assert sb.call("job-status", "j1")[0] == 0
    assert not (sb.work_dir("j1") / ".usage-j1.json").exists()


def test_t40_stop_and_list(sb):
    assert sb.start("j1")[0] == 0
    assert sb.start("j2")[0] == 0
    code, out = sb.call("job-list")
    assert code == 0 and {j["job_id"] for j in out["jobs"]} == {"j1", "j2"} and all(j["state"] == "running" for j in out["jobs"])
    code, out = sb.call("job-stop", "j1")
    assert code == 0 and out["stopped"] is True
    stop = [c["argv"] for c in sb.fd.calls() if c["argv"][0] == "stop"]
    assert stop == [["stop", "-t", "20", "ax-job-j1"]]
    code, out = sb.call("job-stop", "j1")  # 冪等
    assert code == 0
    assert {j["job_id"] for j in sb.call("job-list")[1]["jobs"]} == {"j2"}


def test_t40_detached_environment_refuses(sb):
    (sb.env_home / "DETACHED").write_text("")
    code, out = sb.call("job-list")
    assert code == 5 and out["error"]["class"] == "detached"


def test_t40_runner_defaults_can_be_tuned_by_runner_file(sb):
    p = sb.ax_home / "config" / "runners" / "verify-python.yaml"
    p.write_text(p.read_text() + "memory: 8g\ncpus: '4'\npids_limit: 1024\n")
    assert sb.start("v1", runner="verify-python", model=None, verify="true")[0] == 0
    a = sb.run_args("v1")
    assert flag_values(a, "--memory") == ["8g"] and flag_values(a, "--cpus") == ["4"] and flag_values(a, "--pids-limit") == ["1024"]


def test_t40_runner_with_reserved_env_is_refused(sb):
    p = sb.ax_home / "config" / "runners" / "verify-python.yaml"
    p.write_text(p.read_text().replace("env:\n", "env:\n  HTTP_PROXY: http://evil\n"))
    code, out = sb.start("v1", runner="verify-python", model=None, verify="true")
    assert code == 70 and out["error"]["reason"] == "runner_invalid"
    assert sb.run_calls() == []

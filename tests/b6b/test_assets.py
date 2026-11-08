"""B6b の成果物の静的な検査: runner の定義、登録簿、Dockerfile、compose.d/b6b.yaml、install.sh、ext の schema。

docker のデーモンには触れない（docker compose config は DOCKER_HOST を存在しない先にして、読み込みと解決だけ）。イメージのビルドは未実行。
"""
import json
import os
import re
import shlex
import shutil
import subprocess

import jsonschema
import pytest
import yaml

from tests.b6b.helpers import LAUNCHER_PY, ROOT, load_launcher

IMG = ROOT / "runtime" / "images"
HAS_DOCKER = shutil.which("docker") is not None
RUNNERS = sorted((ROOT / "config" / "runners").glob("*.yaml"))


def schema(name):
    return json.loads((ROOT / "schemas" / "ext" / "b6b" / name).read_text())


def test_ext_schemas_are_valid_json_schema():
    for p in sorted((ROOT / "schemas" / "ext" / "b6b").glob("*.json")):
        s = json.loads(p.read_text())
        jsonschema.Draft202012Validator.check_schema(s)
        assert s["$id"] == f"https://ax.invalid/schemas/ext/b6b/{p.name}"


def test_all_runners_present_and_valid():
    names = {p.stem for p in RUNNERS}
    assert names == {"agent-cli", "agent-cli-node", "agent-cli-python", "agent-cli-dotnet", "chat-json",
                     "verify-node", "verify-python", "verify-dotnet", "verify-ax-ci"}
    v = jsonschema.Draft202012Validator(schema("runner-b6b.schema.json"))
    for p in RUNNERS:
        d = yaml.safe_load(p.read_text())
        assert not list(v.iter_errors(d)), p.name
        assert d["name"] == p.stem


def test_runner_kinds_images_and_commands():
    mod = load_launcher()
    for p in RUNNERS:
        d = yaml.safe_load(p.read_text())
        words = mod.render_command(d["command"], {"model": "m/x", "job_id": "j", "session_arg": "--session-id 11111111-2222-3333-4444-555555555555", "verify": "true"})
        assert words, p.name
        if d["kind"] == "agent-cli":
            assert d["image"].startswith("ax/agent-cli") and words[:2] == ["sh", "-c"]
            for must in ("claude --model m/x -p --output-format json --dangerously-skip-permissions", "/work/.ax-prompt/user.md", "--exclude-dynamic-system-prompt-sections", "--append-system-prompt-file /work/.ax-prompt/system.md"):
                assert must in words[2], (p.name, must)
            assert "--fork-session" not in words[2]
            env = d["env"]
            for n in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
                assert env[n] == "{model}"
        elif d["kind"] == "chat-json":
            assert d["image"].startswith("ax/chat-json") and words == ["python", "-m", "axcommon.chat_json"]
        else:
            assert d["kind"] == "verify" and words == ["sh", "-c", "true"] and "{verify}" in d["command"]
        assert d["image"].endswith(":${AX_TAG}") or d["name"] == "verify-ax-ci"
        # launcher が固定で渡す名前は runner の env に書かない（launcher が拒否する）
        assert not (set(d.get("env", {})) & mod.RESERVED_ENV)
        # 秘密を書かない
        assert not re.search(r"(sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~+/=-]{20,}|token\s*[:=]\s*\S{8,})", p.read_text(), re.I)


def test_dotnet_runners_carry_the_build_environment():
    for n in ("verify-dotnet", "agent-cli-dotnet"):
        env = yaml.safe_load((ROOT / "config" / "runners" / f"{n}.yaml").read_text())["env"]
        assert env["GameDir"] == "/mnt/sod-gamedata/Shape of Dreams" and env["ModDeployDir"] == "/tmp-hdd/moddeploy"


def test_verify_runners_do_not_get_model_credentials():
    mod = load_launcher()
    for n in ("verify-node", "verify-python", "verify-dotnet", "verify-ax-ci"):
        d = yaml.safe_load((ROOT / "config" / "runners" / f"{n}.yaml").read_text())
        assert d["kind"] not in mod.KINDS_WITH_MODEL


# --- Dockerfile ---
def dockerfile(*parts):
    return (IMG.joinpath(*parts) / "Dockerfile").read_text()


def test_agent_cli_dockerfile_matches_f191():
    t = dockerfile("agent-cli")
    body = "\n".join(l for l in t.splitlines() if not l.startswith("#"))
    assert re.search(r"^ARG BASE=debian:trixie-slim@sha256:[0-9a-f]{64}\nFROM \${BASE}\nUSER root", body, re.M)
    for pkg in ("git", "ca-certificates", "openssh-client", "ripgrep", "curl", "jq", "python3"):
        assert pkg in body
    assert "COPY vendor/claude /usr/local/bin/claude" in body
    assert "ENV DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in body
    assert "WORKDIR /work" in body
    assert (IMG / "agent-cli" / "CLAUDE_VERSION").read_text().strip() == "2.1.284"
    assert "vendor/" in (IMG / "agent-cli" / ".gitignore").read_text()
    assert not (IMG / "agent-cli" / "vendor").exists()  # 実体（約232 MB）は git に置かない


def test_verify_dockerfiles_match_f192():
    node, py, net = dockerfile("verify", "node"), dockerfile("verify", "python"), dockerfile("verify", "dotnet")
    assert "pnpm@10.34.6" in node and "python3 make g++" in node and "node:22-trixie-slim" in node and "COPY --from=uvbin /uv /uvx /usr/local/bin/" in node
    assert "python:3.13-slim" in py and "COPY --from=uvbin /uv /uvx /usr/local/bin/" in py
    assert "mcr.microsoft.com/dotnet/sdk:10.0" in net and "git python3" in net and 'ln -sf "$(command -v python3)" /usr/local/bin/python' in net
    for t in (node, py, net):
        assert "apt-get clean" in t or "FROM" in t


def test_base_images_are_parameterised_and_listed_in_the_lock():
    lock = (IMG / "base-images.lock").read_text()
    import configparser

    pins = configparser.ConfigParser()
    pins.read_string(lock)
    listed = set()
    for section in pins.sections():
        pin = pins[section]
        image, digest = pin["image"].rsplit("@", 1)
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        assert pin["digest"] == digest
        assert pin["status"] == "verified"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", pin["checked_at"])
        listed.add(pin["image"])
    refs = set()
    for p in IMG.rglob("Dockerfile"):
        for line in p.read_text().splitlines():
            m = re.match(r"^ARG (?:BASE|[A-Z_]*IMAGE|PYTHON_REF)=(\S+)$", line)
            if m:
                refs.add(m.group(1))
            f = re.match(r"^FROM (\S+)", line)
            if f:
                assert f.group(1).startswith("${") , (p, line)  # FROM は ARG 経由だけ（固定の入れ替えができる）
    assert refs and refs <= listed, refs - listed


def test_chat_json_dockerfile():
    t = dockerfile("chat-json")
    assert "python:3.12-slim" in t and "COPY axcommon /ax/axcommon" in t and 'CMD ["python", "-m", "axcommon.chat_json"]' in t
    assert 'test -n "${JSONSCHEMA_VERSION}"' in t
    ig = (IMG / "chat-json" / "Dockerfile.dockerignore").read_text().splitlines()
    assert ig[0] == "*" and "!axcommon/" in ig  # axcommon 以外は context に入れない（秘密・profile・docs を入れない）


# --- compose ---
def compose_config():
    env = dict(os.environ, DOCKER_HOST="unix:///nonexistent")
    files = ["-f", str(ROOT / "compose.yaml")] + [x for f in sorted((ROOT / "compose.d").glob("*.yaml")) for x in ("-f", str(f))]
    r = subprocess.run(["docker", "compose", "-p", "ax-ci", "--project-directory", str(ROOT), "--env-file", str(ROOT / "tools/ci-fixtures/ci.env"), *files,
                        "--profile", "*", "config", "--format", "json"], capture_output=True, text=True, env=env)
    return r


@pytest.mark.skipif(not HAS_DOCKER, reason="docker の CLI が無い")
def test_compose_b6b_service_definitions():
    r = compose_config()
    assert r.returncode == 0, r.stderr
    assert "variable is not set" not in r.stderr
    cfg = json.loads(r.stdout)
    ep = cfg["services"]["egress-proxy"]
    assert "ports" not in ep or not ep["ports"]                      # 外へ待ち受けない
    assert set(ep["networks"]) == {"ax-work", "ax-job"}              # 両方に足を持つ（F-18.4）
    assert ep["networks"]["ax-work"]["ipv4_address"] == "198.51.100.2"
    assert ep["read_only"] is True and ep["cap_drop"] == ["ALL"] and "no-new-privileges:true" in ep["security_opt"]
    assert ep["user"] == "1000:1000"
    vols = {v["target"]: v for v in ep["volumes"]}
    assert vols["/egress"]["read_only"] is True and vols["/egress"]["source"].endswith("/state/egress")  # 許可ファイルは ro
    assert not any("docker.sock" in str(v.get("source", "")) for v in ep["volumes"])
    assert ep["environment"]["EGRESS_ALLOW_DIR"] == "/egress" and ep["environment"]["EGRESS_MODEL_GATEWAY"] == "http://198.51.100.1:3425"
    assert ep["command"] == ["python", "/ax/core/egress-proxy/egress_proxy.py"]
    assert "secrets" not in ep or not ep["secrets"]                  # 秘密を持たない（モデルの鍵は中継するだけ）
    # ジョブ用コンテナの網: internal、docker run --network ax-job が引ける名前
    assert cfg["networks"]["ax-job"]["internal"] is True and cfg["networks"]["ax-job"]["name"] == "ax-job"
    builds = {n: s for n, s in cfg["services"].items() if n.startswith("img-")}
    # img-runtime は compose.d/b7.yaml が足す共通イメージ（b7 束の所有）。ここでは b6b 束の 8 件が
    # 含まれることと、全体がこの 9 件を上回らないことを判定する（全束を合わせた集合）。
    assert {"img-verify-node", "img-verify-python", "img-verify-dotnet", "img-agent-cli", "img-agent-cli-node", "img-agent-cli-python",
            "img-agent-cli-dotnet", "img-chat-json"} <= set(builds)
    assert set(builds) == {"img-verify-node", "img-verify-python", "img-verify-dotnet", "img-agent-cli", "img-agent-cli-node", "img-agent-cli-python",
                           "img-agent-cli-dotnet", "img-chat-json", "img-runtime"}
    for n, s in builds.items():
        assert s["profiles"] == ["build"] and "ports" not in s and "networks" not in s or not s.get("ports")
    assert builds["img-agent-cli-node"]["build"]["args"]["BASE"] == "ax/verify-node:ci"
    assert builds["img-chat-json"]["build"]["dockerfile"] == "runtime/images/chat-json/Dockerfile"
    # 起動する物（profiles なし）は egress-proxy だけがこの束から増える
    default = {n for n, s in cfg["services"].items() if not s.get("profiles")}
    assert "egress-proxy" in default and not (default & set(builds))


def test_compose_b6b_text_rules():
    t = (ROOT / "compose.d" / "b6b.yaml").read_text()
    body = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    assert "ports:" not in body and "docker.sock" not in body and "privileged" not in body and "0.0.0.0" not in body


# --- install.sh ---
INSTALL = ROOT / "host" / "env-module-min" / "install.sh"


def run_install(dest, *args):
    return subprocess.run(["bash", str(INSTALL), "--dest", str(dest), *args], capture_output=True, text=True)


def test_install_places_release_idempotently(tmp_path):
    dest = tmp_path / "ax-env"
    assert run_install(dest, "--check").returncode == 1                # まだ置いていない
    r = run_install(dest)
    assert r.returncode == 0, r.stderr
    assert os.readlink(dest / "current") == "releases/b6" and os.readlink(dest / "bin") == "current/bin"
    assert (dest / "bin" / "ax-env-run").exists() and os.access(dest / "bin" / "ax-env-run", os.X_OK)
    assert (dest / "releases" / "b6" / "lib" / "ax_launcher.py").is_file()
    assert oct(dest.stat().st_mode & 0o777) == "0o700"
    assert run_install(dest, "--check").returncode == 0
    assert run_install(dest).returncode == 0                           # 2 回目も同じ
    assert not list((dest / "archive").iterdir())                      # 内容が同じなら何も動かさない


def test_install_moves_a_different_release_to_archive_not_delete(tmp_path):
    dest = tmp_path / "ax-env"
    assert run_install(dest).returncode == 0
    (dest / "releases" / "b6" / "lib" / "ax_launcher.py").write_text("# 手で書き換えられた\n")
    r = run_install(dest)
    assert r.returncode == 0, r.stderr
    moved = list((dest / "archive").glob("b6-*"))
    assert len(moved) == 1 and (moved[0] / "lib" / "ax_launcher.py").read_text() == "# 手で書き換えられた\n"
    assert run_install(dest, "--check").returncode == 0


def test_install_does_not_switch_away_from_a_newer_release(tmp_path):
    dest = tmp_path / "ax-env"
    (dest / "releases" / "1.0.0").mkdir(parents=True)
    os.symlink("releases/1.0.0", dest / "current")
    r = run_install(dest)
    assert r.returncode == 1 and "切り替えない" in r.stderr
    assert os.readlink(dest / "current") == "releases/1.0.0"
    assert run_install("relative/dir").returncode == 2


def test_installed_copy_runs_from_its_own_location(tmp_path):
    dest = tmp_path / "ax-env"
    assert run_install(dest).returncode == 0
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "AX_ENV_SUDO": "", "AX_ENV_DOCKER": "/nonexistent-docker-for-test", "SSH_ORIGINAL_COMMAND": "job-list"}
    r = subprocess.run([str(dest / "bin" / "ax-env-run"), "--caller", "launcher"], capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert r.returncode == 70 and out["error"]["reason"] == "docker_unavailable"  # docker が無いので内部の失敗。実物の docker は呼んでいない
    (dest / "DETACHED").write_text("")  # AX_ENV_HOME は置いた場所から引く
    r = subprocess.run([str(dest / "bin" / "ax-env-run"), "--caller", "launcher"], capture_output=True, text=True, env=env)
    assert r.returncode == 5


# --- 静的な禁止 ---
def test_launcher_source_has_no_embedded_hosts_or_forbidden_flags():
    t = LAUNCHER_PY.read_text()
    code = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    for bad in ("--cap-add", "--privileged", "--pid=host", "--net=host", "--device", "shell=True", "os.system", "/var/run/docker.sock"):
        assert bad not in code, bad
    ips = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", code))
    assert not ips
    assert "tailscale" not in code.lower()


def test_launcher_profile_file_default_and_explicit_precedence(tmp_path):
    launcher = load_launcher()
    home = tmp_path / "home"
    default = home / ".config" / "ax" / "profile" / "default.yaml"
    alternative = tmp_path / "alternative.yaml"
    explicit = tmp_path / "explicit.yaml"

    assert str(launcher.Config({"HOME": str(home)}).profile_path) == str(default)
    assert str(launcher.Config({"HOME": str(home), "AX_PROFILE_FILE": str(alternative)}).profile_path) == str(alternative)
    assert str(launcher.Config({"HOME": str(home), "AX_PROFILE_FILE": str(alternative), "AX_PROFILE": str(explicit)}).profile_path) == str(explicit)


def test_chat_json_response_schema_accepts_what_the_client_writes(tmp_path):
    from tests.b6b.test_chat_json import Rec, reply, setup

    from axcommon import chat_json as cj

    v = jsonschema.Draft202012Validator(schema("chat-json-response.schema.json"))
    env = setup(tmp_path)
    for rec in (Rec(body=reply('{"a": 1}')), Rec(body=reply("nope")), Rec(status=429, body={"error": "x"}), Rec(status=500, body={})):
        cj.run(tmp_path, env, rec)
        assert not list(v.iter_errors(json.loads((tmp_path / "response.json").read_text())))

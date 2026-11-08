"""T-120 ホストのスクリプトの最小版（B0）: host/ax-env.sh、host/ax-up.sh、host/ax-redact-index.sh。

profile/example.yaml から F-18.6 の対応表どおりの環境変数ファイルを作り、compose.yaml が使うすべての変数が埋まる
（`docker compose config` に「未設定」の警告が出ない）。索引は先頭がソルト、権限0644。ax-up.sh は偽の sudo/docker で
起動、healthz の待ち、通ったときだけ見張りの登録を確かめる。profile の値が不正なら、何も書かずに終了コード1。
実際の sudo と docker は使わない（AX_UP_SUDO に偽のスクリプトを渡す）。
"""

import ipaddress as _fixture_ipaddress
_NET_FIXTURE_3001 = str(_fixture_ipaddress.ip_network("100.64.0.0/10")[3001])
import hashlib
import os
import re
import shutil
import stat
import subprocess

import pytest
import yaml

from axcommon.profile import MAPPING, REQUIRED
from axcommon.redact_index import value_hash

HAS_DOCKER = shutil.which("docker") is not None


def sh(repo_root, script, *args, env=None, input=None):
    e = {k: v for k, v in os.environ.items()}
    e.update(env or {})
    return subprocess.run(["bash", str(repo_root / "host" / script), *args], capture_output=True, text=True, env=e, input=input)


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture
def profile(repo_root, tmp_path):
    """example.yaml の保存先を tmp_path に向けた profile（ファイルのパスと、書き換えた dict を返す）。"""
    prof = yaml.safe_load((repo_root / "profile" / "example.yaml").read_text())
    prof["secrets_dir"] = str(tmp_path / "secrets")
    prof["tmp_dir"] = str(tmp_path / "tmp")
    prof["ax_state_dir"] = str(tmp_path / "state")
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(prof, allow_unicode=True))
    (tmp_path / "secrets").mkdir()

    def write(p=None):
        path.write_text(yaml.safe_dump(p if p is not None else prof, allow_unicode=True))
        return path

    return path, prof, write


def read_env(path):
    out = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


# ================= ax-env.sh =================
def test_env_from_example_profile_follows_the_f186_table(repo_root, tmp_path):
    out = tmp_path / "profile.env"
    r = sh(repo_root, "ax-env.sh", "--profile", str(repo_root / "profile" / "example.yaml"), "--out", str(out), env={"HOME": "/home/userx-test", "AX_HOME": "/opt/ax", "AX_ENV_ID": "false"})
    assert r.returncode == 0, r.stderr
    env = read_env(out)
    assert env["AX_HOST"] == "example-host"
    assert env["AX_BIND_ADDR"] == "127.0.0.1"
    assert env["AX_SECRETS_DIR"] == "/home/userx-test/.config/ax/secrets"  # ~ の展開
    assert env["AX_STATE_DIR"] == "/home/userx-test/.local/state/ax"
    assert env["AX_TMP_DIR"] == yaml.safe_load((repo_root / "profile" / "example.yaml").read_text())["tmp_dir"]
    assert env["AX_COMMANDER_DIR"] == "/home/userx-test/.local/state/ax/commander"  # ソケットのファイルでなく dirname
    assert env["AX_ENVIRONMENTS_FILE"] == "config/environments.yaml" and env["AX_MODELS_FILE"] == "config/models.yaml"  # 相対のまま
    assert env["AX_ARCHIVE_TARGET"] == "example-archive" and env["AX_NTFY_TOPIC"] == "hub"
    assert env["AX_MODEL_GATEWAY"] == "https://model-gateway.example.invalid/"
    assert env["AX_HOME"] == "/opt/ax"
    assert env["AX_UID"] == str(os.getuid()) and env["AX_GID"] == str(os.getgid())
    assert env["AX_HQ_UID"] == "" and env["AX_HQ_GID"] == ""  # 偽の id がユーザー不在を返す
    assert env["AX_GATE_PORT"] == "8909"
    assert "AX_TAG" not in env  # current.env の変数（F-18.6）


def test_env_file_is_mode_600_and_has_no_temp_leftovers(repo_root, tmp_path):
    out = tmp_path / "d" / "profile.env"
    assert sh(repo_root, "ax-env.sh", "--profile", str(repo_root / "profile" / "example.yaml"), "--out", str(out)).returncode == 0
    assert mode(out) == 0o600
    assert [p.name for p in out.parent.iterdir()] == ["profile.env"]


def test_env_default_output_is_ax_state_dir_deploy_profile_env(repo_root, profile):
    path, prof, _w = profile
    r = sh(repo_root, "ax-env.sh", "--profile", str(path))
    assert r.returncode == 0, r.stderr
    assert (os.path.join(prof["ax_state_dir"], "deploy", "profile.env")) and os.path.isfile(os.path.join(prof["ax_state_dir"], "deploy", "profile.env"))


def test_env_expands_state_dir_reference_in_other_items(repo_root, profile, tmp_path):
    path, prof, _w = profile
    out = tmp_path / "o.env"
    assert sh(repo_root, "ax-env.sh", "--profile", str(path), "--out", str(out)).returncode == 0
    assert read_env(out)["AX_COMMANDER_DIR"] == str(tmp_path / "state" / "commander")


def test_env_does_not_print_values(repo_root, profile, tmp_path):
    path, prof, _w = profile
    r = sh(repo_root, "ax-env.sh", "--profile", str(path), "--out", str(tmp_path / "o.env"))
    assert prof["model_gateway"] not in r.stdout + r.stderr and prof["secrets_dir"] not in r.stdout + r.stderr


def test_env_check_writes_nothing(repo_root, profile, tmp_path):
    path, _p, _w = profile
    out = tmp_path / "never.env"
    r = sh(repo_root, "ax-env.sh", "--profile", str(path), "--check", "--out", str(out))
    assert r.returncode == 0 and not out.exists()


@pytest.mark.parametrize("key", REQUIRED)
def test_env_missing_required_item_fails_with_1_and_writes_nothing(repo_root, profile, tmp_path, key):
    path, prof, write = profile
    del prof[key]
    write(prof)
    out = tmp_path / "o.env"
    r = sh(repo_root, "ax-env.sh", "--profile", str(path), "--out", str(out))
    assert r.returncode == 1 and not out.exists()
    assert key in r.stderr


@pytest.mark.parametrize("key", ["secrets_dir", "tmp_dir", "ax_state_dir", "commander_socket"])
def test_env_relative_path_fails_with_1_and_keeps_existing_file(repo_root, profile, tmp_path, key):
    path, prof, write = profile
    prof[key] = "relative/dir"
    write(prof)
    out = tmp_path / "o.env"
    out.write_text("AX_OLD=keep\n")
    r = sh(repo_root, "ax-env.sh", "--profile", str(path), "--out", str(out))
    assert r.returncode == 1 and out.read_text() == "AX_OLD=keep\n"


def test_env_empty_or_blank_bind_addr_fails(repo_root, profile, tmp_path):
    path, prof, write = profile
    for bad in ("", "   "):
        prof["bind_addr"] = bad
        write(prof)
        out = tmp_path / "o.env"
        assert sh(repo_root, "ax-env.sh", "--profile", str(path), "--out", str(out)).returncode == 1 and not out.exists()


def test_env_absolute_environments_file_is_rejected(repo_root, profile, tmp_path):
    path, prof, write = profile
    prof["environments_file"] = "/etc/passwd"
    write(prof)
    assert sh(repo_root, "ax-env.sh", "--profile", str(path), "--out", str(tmp_path / "o.env")).returncode == 1


def test_env_missing_or_malformed_profile_fails(repo_root, tmp_path):
    assert sh(repo_root, "ax-env.sh", "--profile", str(tmp_path / "none.yaml"), "--out", str(tmp_path / "o.env")).returncode == 1
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n")
    assert sh(repo_root, "ax-env.sh", "--profile", str(bad), "--out", str(tmp_path / "o.env")).returncode == 1
    bad.write_text("a: [unclosed\n")
    assert sh(repo_root, "ax-env.sh", "--profile", str(bad), "--out", str(tmp_path / "o.env")).returncode == 1


def test_env_unknown_option_is_usage_error(repo_root):
    assert sh(repo_root, "ax-env.sh", "--bogus").returncode == 2


def test_env_default_profile_is_default_yaml_without_ax_profile_file(repo_root, tmp_path):
    home = tmp_path / "home"
    default = home / ".config" / "ax" / "profile" / "default.yaml"
    default.parent.mkdir(parents=True)
    default.write_text((repo_root / "profile" / "example.yaml").read_text())
    out = tmp_path / "o.env"
    r = sh(repo_root, "ax-env.sh", "--out", str(out), env={"HOME": str(home), "AX_HOME": "/opt/ax"})
    assert r.returncode == 0, r.stderr
    assert read_env(out)["AX_HOST"] == "example-host"
    assert read_env(out)["AX_ARCHIVE_TARGET"] == "example-archive"


def test_env_default_profile_uses_ax_profile_file_when_no_profile_flag(repo_root, tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "ax" / "profile").mkdir(parents=True)
    alt = tmp_path / "alt.yaml"
    prof = yaml.safe_load((repo_root / "profile" / "example.yaml").read_text())
    prof["host"] = "alt-host"
    alt.write_text(yaml.safe_dump(prof, allow_unicode=True))
    out = tmp_path / "o.env"
    r = sh(repo_root, "ax-env.sh", "--out", str(out), env={"HOME": str(home), "AX_HOME": "/opt/ax", "AX_PROFILE_FILE": str(alt)})
    assert r.returncode == 0, r.stderr
    assert read_env(out)["AX_HOST"] == "alt-host"


def test_env_profile_flag_beats_ax_profile_file(repo_root, tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "ax" / "profile").mkdir(parents=True)
    alt = tmp_path / "alt.yaml"
    prof = yaml.safe_load((repo_root / "profile" / "example.yaml").read_text())
    prof["host"] = "alt-host"
    alt.write_text(yaml.safe_dump(prof, allow_unicode=True))
    out = tmp_path / "o.env"
    r = sh(repo_root, "ax-env.sh", "--profile", str(repo_root / "profile" / "example.yaml"),
           "--out", str(out), env={"HOME": str(home), "AX_HOME": "/opt/ax", "AX_PROFILE_FILE": str(alt)})
    assert r.returncode == 0, r.stderr
    assert read_env(out)["AX_HOST"] == "example-host"


def test_ci_env_variables_equal_generated_env_plus_tag(repo_root, ci_env, tmp_path):
    out = tmp_path / "o.env"
    assert sh(repo_root, "ax-env.sh", "--profile", str(repo_root / "profile" / "example.yaml"), "--out", str(out)).returncode == 0
    assert set(ci_env) == set(read_env(out)) | {"AX_TAG", "AX_CONVERSATION_HISTORY_HOST_DIR", "AX_EMBEDDING_TOKEN_HOST_FILE"}


@pytest.mark.skipif(not HAS_DOCKER, reason="docker の CLI が無い")
def test_generated_env_resolves_compose_without_unset_variable_warning(repo_root, tmp_path):
    out = tmp_path / "profile.env"
    cur = tmp_path / "current.env"
    cur.write_text("AX_TAG=ci\n")
    assert sh(repo_root, "ax-env.sh", "--profile", str(repo_root / "profile" / "example.yaml"), "--out", str(out)).returncode == 0
    files = ["-f", str(repo_root / "compose.yaml")] + [x for p in sorted((repo_root / "compose.d").glob("*.yaml")) for x in ("-f", str(p))]
    env = {**os.environ, "DOCKER_HOST": "unix:///nonexistent"}
    r = subprocess.run(["docker", "compose", "-p", "ax", "--project-directory", str(repo_root), "--env-file", str(out), "--env-file", str(cur), *files,
                        "--profile", "*", "config", "--format", "json"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "variable is not set" not in r.stderr and "WARN" not in r.stderr


# ================= ax-redact-index.sh =================
@pytest.fixture
def secrets_tree(tmp_path):
    s = tmp_path / "secrets"
    (s / "archive").mkdir(parents=True)
    (s / "hq-slots").mkdir()
    (s / "plugin-tokens").mkdir()
    (s / "k1").write_text("abcdefgh12345\n")
    (s / "short").write_text("tiny\n")
    (s / "multi").write_text("first-secret-line\n  second-secret-line  \nab\n")
    (s / "archive" / "old").write_text("archived-secret-value\n")
    (s / "hq-slots" / "slot").write_text("slot-secret-value")
    (s / "plugin-tokens" / "issue-automation").write_text("p" * 64)
    os.symlink("/etc/hostname", s / "link-to-outside")
    (tmp_path / "state").mkdir()
    return s


def run_index(repo_root, secrets, state, **kw):
    return sh(repo_root, "ax-redact-index.sh", "--secrets-dir", str(secrets), "--state-dir", str(state), **kw)


def read_index(state):
    lines = (state / "redact" / "index").read_text().splitlines()
    return lines[0], lines[1:]


def test_index_first_line_is_16_byte_hex_salt_mode_0644(repo_root, secrets_tree, tmp_path):
    state = tmp_path / "state"
    assert run_index(repo_root, secrets_tree, state).returncode == 0
    salt, hashes = read_index(state)
    assert re.fullmatch(r"[0-9a-f]{32}", salt)
    assert mode(state / "redact" / "index") == 0o644
    assert hashes == sorted(set(hashes)) and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)


def test_index_contains_whole_file_and_each_line_of_8_bytes_or_more(repo_root, secrets_tree, tmp_path):
    state = tmp_path / "state"
    run_index(repo_root, secrets_tree, state)
    salt, hashes = read_index(state)
    h = set(hashes)
    assert value_hash(salt, "abcdefgh12345") in h
    assert value_hash(salt, "first-secret-line") in h and value_hash(salt, "second-secret-line") in h  # 行は前後の空白を除く
    assert value_hash(salt, "first-secret-line\n  second-secret-line  \nab") in h  # ファイル全体（末尾の改行を除く）


def test_index_excludes_values_shorter_than_8_bytes(repo_root, secrets_tree, tmp_path):
    state = tmp_path / "state"
    run_index(repo_root, secrets_tree, state)
    salt, hashes = read_index(state)
    assert value_hash(salt, "tiny") not in hashes and value_hash(salt, "ab") not in hashes


def test_index_includes_archive_hq_slots_and_plugin_tokens(repo_root, secrets_tree, tmp_path):
    state = tmp_path / "state"
    run_index(repo_root, secrets_tree, state)
    salt, hashes = read_index(state)
    for v in ("archived-secret-value", "slot-secret-value", "p" * 64):
        assert value_hash(salt, v) in hashes


def test_index_does_not_follow_symlinks(repo_root, secrets_tree, tmp_path):
    state = tmp_path / "state"
    host = open("/etc/hostname").read().strip()
    run_index(repo_root, secrets_tree, state)
    salt, hashes = read_index(state)
    assert len(host.encode()) < 8 or value_hash(salt, host) not in hashes


def test_index_keeps_the_salt_across_rebuilds_and_picks_up_new_values(repo_root, secrets_tree, tmp_path):
    state = tmp_path / "state"
    run_index(repo_root, secrets_tree, state)
    salt1, h1 = read_index(state)
    (secrets_tree / "newfile").write_text("brand-new-secret-value")
    run_index(repo_root, secrets_tree, state)
    salt2, h2 = read_index(state)
    assert salt1 == salt2 and value_hash(salt2, "brand-new-secret-value") in h2 and set(h1) < set(h2)


def test_index_holds_no_plaintext_and_stdout_shows_no_values(repo_root, secrets_tree, tmp_path):
    state = tmp_path / "state"
    r = run_index(repo_root, secrets_tree, state)
    body = (state / "redact" / "index").read_text()
    for v in ("abcdefgh12345", "first-secret-line", "slot-secret-value"):
        assert v not in body and v not in r.stdout + r.stderr


def test_index_hash_is_sha256_of_salt_bytes_plus_utf8_value(repo_root, secrets_tree, tmp_path):
    state = tmp_path / "state"
    run_index(repo_root, secrets_tree, state)
    salt, hashes = read_index(state)
    assert hashlib.sha256(salt.encode() + "abcdefgh12345".encode()).hexdigest() in hashes


def test_index_missing_secrets_dir_fails_and_does_not_touch_existing_index(repo_root, tmp_path):
    state = tmp_path / "state"
    (state / "redact").mkdir(parents=True)
    (state / "redact" / "index").write_text("oldsalt\n")
    r = run_index(repo_root, tmp_path / "nope", state)
    assert r.returncode == 1 and (state / "redact" / "index").read_text() == "oldsalt\n"


def test_index_non_utf8_file_is_skipped_not_fatal(repo_root, secrets_tree, tmp_path):
    (secrets_tree / "binary").write_bytes(b"\xff\xfe\x00\x01" * 10)
    state = tmp_path / "state"
    assert run_index(repo_root, secrets_tree, state).returncode == 0


def test_t1_style_lookup_value_in_index_is_found_by_word_hash(repo_root, secrets_tree, tmp_path):
    """T-1 の「索引にある8文字以上の値が [REDACTED] になる」の前提: 語をハッシュして索引にあるかで決める（redact の側は別の束）。"""
    state = tmp_path / "state"
    run_index(repo_root, secrets_tree, state)
    salt, hashes = read_index(state)
    text = "ログに abcdefgh12345 が混ざった"
    words = re.findall(r"[A-Za-z0-9._~+/=:-]{8,}", text)
    assert [w for w in words if value_hash(salt, w) in set(hashes)] == ["abcdefgh12345"]


# ================= ax-up.sh（偽の sudo）=================
FAKE_SUDO = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
if [ "$1" = docker ] && [ "$2" = compose ]; then
  last=""; for a in "$@"; do last="$a"; done
  case " $* " in
    *" ps "*) echo "cid-$last" ;;
    *" up "*) exit "${FAKE_UP_RC:-0}" ;;
    *" config "*)
      if [ "$last" = json ]; then
        python3 - "$@" <<'PYCONFIG'
import json, pathlib, sys
import yaml
networks = {}
for name, subnet, gateway in (("ax-core", "192.0.2.0/25", "192.0.2.1"),
                              ("ax-work", "198.51.100.0/25", "198.51.100.1"),
                              ("ax-publish-review", "192.0.2.128/26", "192.0.2.129"),
                              ("ax-job", "203.0.113.0/25", "203.0.113.1")):
    networks[name] = {"name": "ax_" + name, "ipam": {"config": [{"subnet": subnet, "gateway": gateway}]}}
args = sys.argv[1:]
for i, arg in enumerate(args[:-1]):
    if arg == "-f" and "/ax-ipam-" in args[i + 1] and args[i + 1].endswith(".compose.yaml"):
        overlay = yaml.safe_load(pathlib.Path(args[i + 1]).read_text()) or {}
        for name, value in overlay.get("networks", {}).items():
            networks[name]["ipam"]["config"][0].update(value["ipam"]["config"][0])
print(json.dumps({"name": "ax", "networks": networks, "services": {}}))
PYCONFIG
      else
        if [ -n "${FAKE_CONFIG_JSON:-}" ]; then echo "$FAKE_CONFIG_JSON"; else echo "{}"; fi
      fi ;;
  esac
elif [ "$1" = docker ] && [ "$2" = wait ]; then
  echo "${FAKE_WAIT_RC:-0}"
elif [ "$1" = docker ] && [ "$2" = inspect ]; then
  echo "${FAKE_INSPECT_OUT:-${FAKE_HEALTH:-healthy}}"
fi
exit 0
"""


@pytest.fixture
def up_env(repo_root, profile, tmp_path):
    path, prof, write = profile
    sudo = tmp_path / "fakesudo"
    sudo.write_text(FAKE_SUDO)
    sudo.chmod(0o755)
    home = tmp_path / "axhome"
    (home / "compose.d").mkdir(parents=True)
    (home / "plugins").mkdir()
    shutil.copy(repo_root / "compose.yaml", home / "compose.yaml")
    shutil.copy(repo_root / "compose.d" / "b0.yaml", home / "compose.d" / "b0.yaml")
    (home / "plugins" / "registry.yaml").write_text(yaml.safe_dump({"schema_version": 1, "plugins": [
        {"id": "issue-automation", "kind": "subloop", "path": "x"}, {"id": "worker-pool", "kind": "resource", "path": "x"},
        {"id": "hibi-state", "kind": "timer", "path": "x"}, {"id": "other-timer", "kind": "timer", "path": "x"}]}))
    state = tmp_path / "state"
    (state / "deploy").mkdir(parents=True)
    (state / "deploy" / "current.env").write_text("AX_TAG=testtag\n")  # git を使わない
    log = tmp_path / "sudo.log"
    env = {"AX_UP_SUDO": str(sudo), "AX_HOME": str(home), "FAKE_LOG": str(log), "AX_UP_WAIT_S": "2", "AX_UP_INTERVAL_S": "1", "HOME": str(tmp_path)}

    def run(*services, extra=None, profile_path=None):
        return sh(repo_root, "ax-up.sh", "--profile", str(profile_path or path), *services, env={**env, **(extra or {})})

    return type("U", (), {"run": staticmethod(run), "state": state, "log": log, "home": home, "secrets": tmp_path / "secrets", "path": path, "prof": prof, "write": write})


def test_intake_prepares_seat_identity_and_preserves_existing_data(up_env, tmp_path):
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    fake_id = bindir / "id"
    fake_id.write_text('#!/bin/sh\nif [ "$2" = ax-seat ]; then\n  [ "$1" = -u ] && echo 2401 || echo 2402\nelse\n  exec /usr/bin/id "$@"\nfi\n')
    fake_id.chmod(0o755)
    seat = up_env.state / "seat"
    seat.mkdir()
    (seat / "existing").write_text("keep")
    (up_env.state / "deploy" / "current.env").write_text("AX_TAG=testtag\nAX_UID=12345\n")
    result = up_env.run("intake", extra={"PATH": str(bindir) + os.pathsep + os.environ["PATH"]})
    assert result.returncode == 0, result.stderr
    assert (seat / "existing").read_text() == "keep"
    env = (up_env.state / "deploy" / "profile.env").read_text()
    assert "AX_SEAT_UID=2401\n" in env and "AX_SEAT_GID=2402\n" in env
    log = up_env.log.read_text()
    assert f"chown 12345:2402 {seat}" in log
    assert f"chmod 2710 {seat}" in log


def test_intake_missing_seat_identity_does_not_grant_group_access(up_env, tmp_path):
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    fake_id = bindir / "id"
    fake_id.write_text('#!/bin/sh\n[ "$2" = ax-seat ] && exit 1\nexec /usr/bin/id "$@"\n')
    fake_id.chmod(0o755)
    result = up_env.run("intake", extra={"PATH": str(bindir) + os.pathsep + os.environ["PATH"]})
    assert result.returncode == 0, result.stderr
    assert "seat identity unavailable" in result.stderr
    assert mode(up_env.state / "seat") == 0o700
    assert "AX_SEAT_GID=" not in (up_env.state / "deploy" / "profile.env").read_text()


def test_up_creates_state_dirs_with_required_permissions(up_env):
    (up_env.state / "commander").mkdir()
    os.chmod(up_env.state / "commander", 0o755)  # 既にあれば権限を直す
    assert up_env.run("timeline").returncode == 0
    for d in ("deploy", "commander", "host-requests", "hostwatch/registered", "redact", "jobs", "locks", "cutover"):
        assert (up_env.state / d).is_dir(), d
    assert mode(up_env.state / "commander") == 0o700 and mode(up_env.state / "cutover") == 0o700


def test_up_writes_profile_env_and_runs_compose_up_with_f186_command(up_env):
    r = up_env.run("timeline")
    assert r.returncode == 0, r.stderr
    assert (up_env.state / "deploy" / "profile.env").is_file()
    up_line = [ln for ln in up_env.log.read_text().splitlines() if " up -d " in ln][0]
    home, st = str(up_env.home), str(up_env.state)
    assert up_line.startswith(f"docker compose -p ax --project-directory {home} --env-file {st}/deploy/profile.env --env-file {st}/deploy/current.env -f {home}/compose.yaml -f {home}/compose.d/b0.yaml")
    assert up_line.endswith("up -d timeline")


# 退避元の断片（F-20）: archive-sources.yaml から生成した compose の断片を ax-up.sh が -f に足す
def profile_with_sources(profile, text):
    """profile と同じ場所に archive-sources.yaml を置く（ax-up.sh は profile の隣を読む）。"""
    srcs = profile.path.parent / "archive-sources.yaml"
    srcs.write_text(text, encoding="utf-8")
    return srcs


def test_up_dry_run_includes_generated_sources_fragment_when_sources_exist(up_env, tmp_path):
    (tmp_path / "mp-runs").mkdir()  # 存在する退避元だけが断片に入る
    profile_with_sources(up_env, "sources:\n  - {name: mp-runs-archive, path: %s/mp-runs}\n" % tmp_path)
    r = up_env.run("timeline")
    assert r.returncode == 0, r.stderr
    frag = up_env.state / "deploy" / "archive-sources.compose.yaml"
    assert frag.is_file()
    up_line = [ln for ln in up_env.log.read_text().splitlines() if " up -d " in ln][0]
    assert f"-f {frag}" in up_line
    assert "/src/mp-runs-archive" in frag.read_text()


def test_up_dry_run_has_no_sources_fragment_without_sources(up_env):
    r = up_env.run("timeline")
    assert r.returncode == 0, r.stderr
    assert not (up_env.state / "deploy" / "archive-sources.compose.yaml").exists()
    up_line = [ln for ln in up_env.log.read_text().splitlines() if " up -d " in ln][0]
    assert "archive-sources.compose.yaml" not in up_line


def test_up_keeps_starting_when_fragment_generation_fails(up_env):
    profile_with_sources(up_env, "sources: [{name: Bad, path: /x}]\n")  # 名前が不正で生成が失敗する
    r = up_env.run("timeline")
    assert r.returncode == 0, r.stderr
    assert not (up_env.state / "deploy" / "archive-sources.compose.yaml").exists()
    up_line = [ln for ln in up_env.log.read_text().splitlines() if " up -d " in ln][0]
    assert "archive-sources.compose.yaml" not in up_line
    assert "mount できない" in r.stderr  # 警告で archiver が退避元を mount できないことを明記する
    assert (up_env.state / "hostwatch" / "registered" / "timeline").is_file()  # 起動自体は続く


def test_up_registers_with_hostwatch_only_after_healthy(up_env):
    r = up_env.run("timeline", "ledger")
    assert r.returncode == 0
    assert (up_env.state / "hostwatch" / "registered" / "timeline").is_file() and (up_env.state / "hostwatch" / "registered" / "ledger").is_file()
    assert (up_env.state / "hostwatch" / "registered" / "timeline").stat().st_size == 0  # 空のファイル


def test_up_does_not_register_when_service_never_becomes_healthy(up_env):
    r = up_env.run("timeline", extra={"FAKE_HEALTH": "starting"})
    assert r.returncode == 1
    assert not (up_env.state / "hostwatch" / "registered" / "timeline").exists()


def test_up_does_not_register_when_compose_up_fails(up_env):
    r = up_env.run("timeline", extra={"FAKE_UP_RC": "1"})
    assert r.returncode == 1 and not (up_env.state / "hostwatch" / "registered" / "timeline").exists()


def test_up_accepts_running_when_service_has_no_healthcheck(up_env):
    assert up_env.run("timeline", extra={"FAKE_HEALTH": "running"}).returncode == 0


# 一度きりのサービス（restart: "no"。archive-init・b3-volume-init 等）は healthy ではなく正常終了（exit 0）を待つ
ONE_SHOT_CONFIG = '{"restart": "no"}'
INSPECT_EXITED_0 = "exited exit=0"


def test_up_waits_for_successful_exit_of_one_shot_service(up_env):
    r = up_env.run("b3-volume-init", extra={"FAKE_CONFIG_JSON": ONE_SHOT_CONFIG, "FAKE_INSPECT_OUT": INSPECT_EXITED_0})
    assert r.returncode == 0, r.stderr
    log = up_env.log.read_text().splitlines()
    assert any(" docker wait " in f" {ln} " for ln in log)
    # 一度きりのサービスも登録する（見張りの対象から外す運用は hostwatch 側の課題で、登録の作り方は同じ）
    assert (up_env.state / "hostwatch" / "registered" / "b3-volume-init").is_file()


def test_up_fails_when_one_shot_service_does_not_finish_successfully(up_env):
    r = up_env.run("b3-volume-init", extra={"FAKE_CONFIG_JSON": ONE_SHOT_CONFIG, "FAKE_INSPECT_OUT": "exited exit=1"})
    assert r.returncode == 1
    assert "正常終了しない" in r.stderr
    assert not (up_env.state / "hostwatch" / "registered" / "b3-volume-init").exists()


def test_up_normal_service_does_not_wait_or_check_exit(up_env):
    # restart: "no" 以外は従来どおり healthy / running の待ちだけ（docker wait を呼ばない）
    r = up_env.run("timeline", extra={"FAKE_CONFIG_JSON": '{"restart": "unless-stopped"}', "FAKE_HEALTH": "running"})
    assert r.returncode == 0, r.stderr
    assert not any(" docker wait " in f" {ln} " for ln in up_env.log.read_text().splitlines())


def test_up_with_invalid_profile_runs_nothing(up_env):
    bad = dict(up_env.prof)
    bad["bind_addr"] = ""
    p = up_env.write(bad)
    r = up_env.run("timeline", profile_path=p)
    assert r.returncode == 1 and not up_env.log.exists()


def test_up_usage_errors(up_env):
    assert up_env.run().returncode == 2
    assert up_env.run("Bad Name").returncode == 2
    assert up_env.run("UPPER").returncode == 2


def test_up_creates_plugin_token_once_hex64_mode600_and_never_prints_it(up_env):
    r = up_env.run("sl-issue-automation")
    assert r.returncode == 0
    tok = up_env.secrets / "plugin-tokens" / "issue-automation"
    value = tok.read_text()
    assert re.fullmatch(r"[0-9a-f]{64}", value) and mode(tok) == 0o600
    assert value not in r.stdout + r.stderr
    up_env.run("sl-issue-automation")
    assert tok.read_text() == value  # 無いときだけ作る


def test_up_plugin_token_hash_list_registers_without_exposing_credential(up_env):
    import hashlib
    r = up_env.run("sl-issue-automation")
    assert r.returncode == 0
    token = (up_env.secrets / "plugin-tokens" / "issue-automation").read_text().strip()
    rows = (up_env.state / "plugin-tokens.sha256").read_text().splitlines()
    assert ["issue-automation", hashlib.sha256(token.encode()).hexdigest()] in [row.split() for row in rows]
    assert token not in r.stdout + r.stderr


def test_up_timer_runner_creates_tokens_for_every_timer_plugin_only(up_env):
    assert up_env.run("timer-runner").returncode == 0
    toks = sorted(p.name for p in (up_env.secrets / "plugin-tokens").iterdir())
    assert toks == ["hibi-state", "other-timer"]


def test_up_registers_scoped_timer_tokens(up_env):
    import hashlib
    reg = up_env.home / "plugins" / "registry.yaml"
    reg.write_text(yaml.safe_dump({"plugins": [
        {"id": "reader", "kind": "timer", "path": "plugins/reader.yaml"},
        {"id": "writer", "kind": "timer", "path": "plugins/writer.yaml"},
        {"id": "both", "kind": "timer", "path": "plugins/both.yaml"},
    ]}))
    for pid, caps in [("reader", ["memory.read"]), ("writer", ["memory.write"]),
                      ("both", ["memory.read", "memory.write"])]:
        (up_env.home / "plugins" / f"{pid}.yaml").write_text(yaml.safe_dump({"id": pid, "memory_capabilities": caps}))
    result = up_env.run("timer-runner")
    assert result.returncode == 0
    rows = (up_env.state / "plugin-tokens.sha256").read_text().splitlines()
    for prefix, pid in [("read", "reader"), ("write", "writer")]:
        value = (up_env.secrets / "plugin-tokens" / f"memory-{prefix}--{pid}").read_text().strip()
        assert f"memory.{prefix}:{pid} {hashlib.sha256(value.encode()).hexdigest()}" in rows
        assert value not in result.stdout + result.stderr
    assert not (up_env.secrets / "plugin-tokens" / "memory-read--both").exists()
    assert not (up_env.secrets / "plugin-tokens" / "memory-write--both").exists()
    assert up_env.run("timer-runner").returncode == 0
    assert (up_env.state / "plugin-tokens.sha256").read_text().splitlines() == rows


def test_up_timer_bind_inputs_unset_or_missing(up_env, tmp_path, repo_root):
    import yaml
    compose = yaml.safe_load((repo_root / 'compose.d/b16a-t60.yaml').read_text())
    sources = [volume['source'] for volume in compose['services']['timer-runner']['volumes']]
    assert sources == ['${AX_CONVERSATION_HISTORY_HOST_DIR:-/dev/null}', '${AX_EMBEDDING_TOKEN_HOST_FILE:-/dev/null}']
    assert up_env.run('timer-runner', extra={'AX_CONVERSATION_HISTORY_HOST_DIR': '', 'AX_EMBEDDING_TOKEN_HOST_FILE': ''}).returncode == 0
    for name in ('AX_CONVERSATION_HISTORY_HOST_DIR', 'AX_EMBEDDING_TOKEN_HOST_FILE'):
        missing = tmp_path / ('missing-' + name)
        result = up_env.run('timer-runner', extra={name: str(missing)})
        assert result.returncode != 0
        assert name in result.stderr
        assert not missing.exists()


def test_up_non_plugin_service_creates_no_plugin_token(up_env):
    up_env.run("timeline")
    assert not (up_env.secrets / "plugin-tokens").exists() or not list((up_env.secrets / "plugin-tokens").iterdir())


@pytest.mark.parametrize(("svc", "component"), [("worker-pool", "worker-pool"), ("resources", "worker-pool"), ("manager", "manager")])
def test_up_creates_core_token_for_needed_service_and_hash_line_once(up_env, svc, component):
    assert up_env.run(svc).returncode == 0
    tok = up_env.secrets / "core-tokens" / component
    value = tok.read_text()
    assert re.fullmatch(r"[0-9a-f]{64}", value) and mode(tok) == 0o600
    lst = up_env.state / "core-tokens.sha256"
    assert lst.read_text() == f"{component} {hashlib.sha256(value.encode()).hexdigest()}\n"
    up_env.run(svc)
    assert lst.read_text().count(f"{component} ") == 1 and tok.read_text() == value


def test_up_builds_redact_index_only_when_missing(up_env):
    (up_env.secrets / "x").write_text("some-secret-value")
    up_env.run("timeline")
    idx = up_env.state / "redact" / "index"
    assert idx.is_file()
    first = idx.read_text()
    (up_env.secrets / "y").write_text("another-secret-value")
    up_env.run("timeline")
    assert idx.read_text() == first  # 既にあれば作り直さない（作り直しは B8）


def test_up_does_not_touch_existing_current_env(up_env):
    up_env.run("timeline")
    assert (up_env.state / "deploy" / "current.env").read_text() == "AX_TAG=testtag\n"


def test_up_never_calls_anything_other_than_sudo_docker(up_env):
    up_env.run("timeline")
    assert all(ln.startswith("docker ") for ln in up_env.log.read_text().splitlines())


def test_up_default_profile_is_default_yaml_without_ax_profile_file(repo_root, profile, tmp_path):
    path, prof, _w = profile
    home = tmp_path / "uphome-a"
    default = home / ".config" / "ax" / "profile" / "default.yaml"
    default.parent.mkdir(parents=True)
    default.write_text(yaml.safe_dump(prof, allow_unicode=True))
    out = home / "o.env"
    r = sh(repo_root, "ax-up.sh", "--dry-run", "timeline",
           env={"HOME": str(home), "AX_HOME": str(up_env_home(repo_root, tmp_path, "uphome-a-build"))})
    assert r.returncode == 0, r.stderr
    assert "ax-up(dry-run):" in r.stdout
    assert str(default) in r.stdout


def test_up_profile_flag_beats_ax_profile_file(repo_root, profile, tmp_path):
    path, prof, _w = profile
    home = tmp_path / "uphome-b"
    home.mkdir()
    alt = tmp_path / "alt-up.yaml"
    alt_prof = dict(prof)
    alt_prof["host"] = "alt-host"
    alt.write_text(yaml.safe_dump(alt_prof, allow_unicode=True))
    r = sh(repo_root, "ax-up.sh", "--dry-run", "--profile", str(path), "timeline",
           env={"HOME": str(home), "AX_HOME": str(up_env_home(repo_root, tmp_path, "uphome-b-build")),
                "AX_PROFILE_FILE": str(alt)})
    assert r.returncode == 0, r.stderr
    assert str(path) in r.stdout and str(alt) not in r.stdout


def test_up_uses_ax_profile_file_when_no_profile_flag(repo_root, profile, tmp_path):
    path, prof, _w = profile
    home = tmp_path / "uphome-c"
    home.mkdir()
    alt = tmp_path / "alt-up.yaml"
    alt_prof = dict(prof)
    alt_prof["host"] = "alt-host"
    alt.write_text(yaml.safe_dump(alt_prof, allow_unicode=True))
    r = sh(repo_root, "ax-up.sh", "--dry-run", "timeline",
           env={"HOME": str(home), "AX_HOME": str(up_env_home(repo_root, tmp_path, "uphome-c-build")),
                "AX_PROFILE_FILE": str(alt)})
    assert r.returncode == 0, r.stderr
    assert str(alt) in r.stdout


def up_env_home(repo_root, tmp_path, name):
    home = tmp_path / name
    (home / "compose.d").mkdir(parents=True)
    shutil.copy(repo_root / "compose.yaml", home / "compose.yaml")
    shutil.copy(repo_root / "compose.d" / "b0.yaml", home / "compose.d" / "b0.yaml")
    return home


@pytest.fixture
def profile_precedence(repo_root, tmp_path):
    home = tmp_path / "profile-home"
    profiles = {"home": home}
    for index, name in enumerate(("default", "environment", "flag"), start=1):
        data = yaml.safe_load((repo_root / "profile" / "example.yaml").read_text())
        root = tmp_path / name
        state = root / "state"
        secrets = root / "secrets"
        temp = root / "tmp"
        state.mkdir(parents=True)
        secrets.mkdir()
        temp.mkdir()
        (secrets / "source").write_text(f"{name}-secret-value\n")
        data["host"] = f"{name}-host"
        data["ax_state_dir"] = str(state)
        data["secrets_dir"] = str(secrets)
        data["tmp_dir"] = str(temp)
        data["env_ssh_targets"] = [{"id": "target", "host": f"{name}.example.invalid", "port": 2200 + index}]
        data["archive_dest"] = None
        path = home / ".config" / "ax" / "profile" / "default.yaml" if name == "default" else root / "profile.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, allow_unicode=True))
        profiles[name] = {"path": path, "data": data, "state": state}
    return profiles


def precedence_env(profiles, selection):
    env = {"HOME": str(profiles["home"])}
    if selection == "environment":
        env["AX_PROFILE_FILE"] = str(profiles["environment"]["path"])
    elif selection == "flag":
        env["AX_PROFILE_FILE"] = str(profiles["environment"]["path"])
    return env


def precedence_args(profiles, selection):
    return ["--profile", str(profiles["flag"]["path"])] if selection == "flag" else []


def test_peers_profile_precedence(repo_root, profile_precedence, tmp_path):
    profiles = profile_precedence
    tailscale = tmp_path / "tailscale-peers"
    status = tmp_path / "tailscale-status.json"
    status.write_text('{"User":{"1":{"LoginName":"test-user-1@example.com"}},"Self":{"UserID":1,"DNSName":"device.example.invalid.","StableID":"device-1","TailscaleIPs":["__NET_FIXTURE_3001__"]}}'.replace('__NET_FIXTURE_3001__', _NET_FIXTURE_3001))
    tailscale.write_text("#!/bin/sh\ncat \"$AX_TEST_TAILSCALE_STATUS\"\n")
    tailscale.chmod(0o755)
    for selection in ("default", "environment", "flag"):
        env = precedence_env(profiles, selection)
        env.update({"AX_PEERS_TAILSCALE": str(tailscale), "AX_TEST_TAILSCALE_STATUS": str(status)})
        r = sh(repo_root, "ax-peers.sh", *precedence_args(profiles, selection), env=env)
        assert r.returncode == 0, r.stderr
        assert (profiles[selection]["state"] / "gate" / "peers.json").is_file()


def test_fw_profile_precedence(repo_root, profile_precedence, tmp_path):
    profiles = profile_precedence
    tailscale = tmp_path / "tailscale-fw"
    tailscale.write_text("#!/bin/sh\nprintf '%s\\n' __NET_FIXTURE_3001__\n".replace('__NET_FIXTURE_3001__', _NET_FIXTURE_3001))
    tailscale.chmod(0o755)
    for selection in ("default", "environment", "flag"):
        env = precedence_env(profiles, selection)
        env["AX_FW_TAILSCALE"] = str(tailscale)
        env.update(AX_FW_RESOURCES_IP="198.51.100.11", AX_FW_ARCHIVER_IP="192.0.2.12",
                   AX_FW_WORKER_POOL_IP="198.51.100.10")
        r = sh(repo_root, "ax-fw.sh", "rules", *precedence_args(profiles, selection), env=env)
        assert r.returncode == 0, r.stderr
        assert f"--dport {profiles[selection]['data']['env_ssh_targets'][0]['port']}" in r.stdout


def test_gate_revoke_profile_precedence(repo_root, profile_precedence):
    profiles = profile_precedence
    for selection in ("default", "environment", "flag"):
        r = sh(repo_root, "ax-gate-revoke.sh", *precedence_args(profiles, selection), "seat-one",
               env=precedence_env(profiles, selection))
        assert r.returncode == 0, r.stderr
        assert (profiles[selection]["state"] / "gate" / "revoked.d" / "seat-one").is_file()


def test_redact_index_profile_precedence(repo_root, profile_precedence):
    profiles = profile_precedence
    for selection in ("default", "environment", "flag"):
        r = sh(repo_root, "ax-redact-index.sh", *precedence_args(profiles, selection),
               env=precedence_env(profiles, selection))
        assert r.returncode == 0, r.stderr
        assert (profiles[selection]["state"] / "redact" / "index").is_file()


def test_hostwatch_config_profile_precedence(repo_root, profile_precedence):
    profiles = profile_precedence

    def load_host(selection):
        env = {k: v for k, v in os.environ.items() if k not in {"AX_PROFILE", "AX_PROFILE_FILE"}}
        env["HOME"] = str(profiles["home"])
        env["PYTHONPATH"] = f"{repo_root}:{repo_root / 'host' / 'lib'}"
        if selection == "environment":
            env["AX_PROFILE_FILE"] = str(profiles["environment"]["path"])
        elif selection == "flag":
            env["AX_PROFILE_FILE"] = str(profiles["environment"]["path"])
            env["AX_PROFILE"] = str(profiles["flag"]["path"])
        return subprocess.run(["python3", "-P", "-c", "from hostwatch.config import load; print(load().host)"],
                              capture_output=True, text=True, env=env)

    for selection in ("default", "environment", "flag"):
        r = load_host(selection)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == f"{selection}-host"


def test_ax_up_includes_noded_secrets_only_when_both_exist(repo_root, profile):
    path, prof, _ = profile
    secrets = __import__('pathlib').Path(prof['secrets_dir'])
    def run():
        return sh(repo_root, 'ax-up.sh', '--profile', str(path), '--dry-run', 'resources',
                  env={'AX_HOME': str(repo_root)})
    assert 'compose.noded-ssh.yaml' not in run().stdout
    (secrets / 'env-ssh-noded').write_text('fake key')
    assert 'compose.noded-ssh.yaml' not in run().stdout
    (secrets / 'env-ssh-noded-known-hosts').write_text('fake host key')
    result = run()
    assert result.returncode == 0, result.stderr
    assert 'compose.noded-ssh.yaml' in result.stdout
    overlay = yaml.safe_load((repo_root / 'host/compose.noded-ssh.yaml').read_text())
    assert {entry['source'] for entry in overlay['services']['resources']['secrets']} == {
        'env-ssh-noded', 'env-ssh-noded-known-hosts'}
    assert all(item['file'].startswith('${AX_SECRETS_DIR}/') for item in overlay['secrets'].values())


PUBLISH_HOST_KEYS = (
    'AX_PUBLISH_REPOSITORIES_HOST_FILE', 'AX_PUBLISH_REPOSITORY_HOST_DIR',
    'AX_PUBLISH_HOSTS_HOST_FILE', 'AX_PUBLISH_TERMS_HOST_FILE',
)


def publish_inputs(tmp_path):
    directory = tmp_path / 'publish inputs'
    directory.mkdir()
    paths = [directory / 'repositories.yaml', directory, directory / 'hosts', directory / 'terms']
    for path in (paths[0], paths[2], paths[3]):
        path.write_text('example')
    return paths


def test_up_publish_launcher_env_and_process_precedence(up_env, tmp_path, monkeypatch):
    for key in PUBLISH_HOST_KEYS:
        monkeypatch.delenv(key, raising=False)
    paths = publish_inputs(tmp_path)
    config = up_env.home / 'config'
    config.mkdir()
    (config / 'launcher.env').write_text('\n'.join(
        f'{key}={quote}{path}{quote}' for key, path, quote in zip(PUBLISH_HOST_KEYS, paths, ("'", '"', "'", '"'))
    ) + '\nUNRELATED_KEY=ignored\n')
    result = up_env.run('sl-release')
    assert result.returncode == 0, result.stderr
    fragment = next((up_env.state / 'deploy').glob('publish-review-*.compose.yaml'))
    volumes = yaml.safe_load(fragment.read_text())['services']['sl-release']['volumes']
    assert [v['source'] for v in volumes] == list(map(str, paths))
    override = tmp_path / 'override'
    override.write_text('example')
    env_file = config / 'launcher.env'
    env_file.write_text(env_file.read_text().replace(f'{PUBLISH_HOST_KEYS[0]}=\'{paths[0]}\'', f'{PUBLISH_HOST_KEYS[0]}=$(invalid)'))
    result = up_env.run('sl-release', extra={PUBLISH_HOST_KEYS[0]: str(override)})
    assert result.returncode == 0, result.stderr
    fragments = sorted((up_env.state / 'deploy').glob('publish-review-*.compose.yaml'), key=lambda path: path.stat().st_mtime_ns)
    fragment = fragments[-1]
    volumes = yaml.safe_load(fragment.read_text())['services']['sl-release']['volumes']
    assert [v['source'] for v in volumes] == [str(override), *map(str, paths[1:])]
    assert all(v['read_only'] is True for v in volumes)
    assert str(fragment) in up_env.log.read_text()


@pytest.mark.parametrize('bad', ['duplicate', 'command', 'multiline', 'relative', 'unknown-expression'])
def test_up_bad_publish_launcher_input_keeps_other_services(up_env, tmp_path, monkeypatch, bad):
    for key in PUBLISH_HOST_KEYS:
        monkeypatch.delenv(key, raising=False)
    paths = publish_inputs(tmp_path)
    config = up_env.home / 'config'
    config.mkdir()
    lines = [f'{key}="{path}"' for key, path in zip(PUBLISH_HOST_KEYS, paths)]
    marker = tmp_path / 'executed'
    if bad == 'duplicate':
        lines.append(lines[0])
    elif bad == 'command':
        lines[0] = f'{PUBLISH_HOST_KEYS[0]}=$(touch {marker})'
    elif bad == 'multiline':
        lines[0] = f'{PUBLISH_HOST_KEYS[0]}="{paths[0]}\nextra"'
    elif bad == 'relative':
        lines[0] = f'{PUBLISH_HOST_KEYS[0]}=relative'
    else:
        lines[0] = f'{PUBLISH_HOST_KEYS[0]}=${{HOME}}/input'
    (config / 'launcher.env').write_text('\n'.join(lines) + '\n')
    result = up_env.run('sl-release', 'timeline')
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert not list((up_env.state / 'deploy').glob('publish-review-*.compose.yaml'))
    assert (up_env.state / 'hostwatch' / 'registered' / 'timeline').exists()
    assert 'publication review inputs unavailable' in result.stderr
    assert str(marker) not in result.stdout + result.stderr


LAUNCHER_FILES = {
    'resources': ('AX_ENVIRONMENTS_HOST_FILE', 'AX_ENVIRONMENTS_FILE', '/run/ax-config/environments.yaml'),
    'sl-release': ('AX_PRODUCTS_HOST_FILE', 'AX_PRODUCTS_FILE', '/run/ax-config/products.yaml'),
    'sl-change': ('AX_CHANGE_TARGETS_HOST_FILE', 'AX_CHANGE_TARGETS_FILE', '/run/ax-config/change-targets.yaml'),
}
URL_PREFIXES = 'AX_SCHEDULED_PUBLISHER_URL_PREFIXES'


def test_up_launcher_optional_inputs_are_isolated_and_passed_to_compose(up_env, tmp_path, monkeypatch):
    for key in (*PUBLISH_HOST_KEYS, *(item[0] for item in LAUNCHER_FILES.values()), URL_PREFIXES):
        monkeypatch.delenv(key, raising=False)
    config = up_env.home / 'config'
    config.mkdir()
    files = {}
    for service in LAUNCHER_FILES:
        files[service] = tmp_path / (service + '.yaml')
        files[service].write_text('example')
    override = tmp_path / 'override.yaml'
    override.write_text('example')
    url = 'https://example.invalid/a/,https://example.org/b/'
    env_lines = [f'{LAUNCHER_FILES[svc][0]}="{path}"' for svc, path in files.items()]
    env_lines.append(f'{URL_PREFIXES}={url}')
    (config / 'launcher.env').write_text('\n'.join(env_lines) + '\n')
    result = up_env.run('resources', 'sl-release', 'sl-change', 'sl-scheduled-publisher',
                        extra={LAUNCHER_FILES['resources'][0]: str(override)})
    assert result.returncode == 0, result.stderr
    for service, (_, variable, target) in LAUNCHER_FILES.items():
        fragment = next((up_env.state / 'deploy').glob(f'launcher-{service}-*.compose.yaml'))
        cfg = yaml.safe_load(fragment.read_text())['services'][service]
        assert cfg['environment'][variable] == target
        assert cfg['volumes'] == [{'type': 'bind', 'source': str(override if service == 'resources' else files[service]),
                                   'target': target, 'read_only': True}]
        assert str(fragment) in up_env.log.read_text()
    url_file = next((up_env.state / 'deploy').glob('launcher-url-*.env'))
    assert url_file.read_text() == f'{URL_PREFIXES}={url}\n'
    assert str(url_file) in up_env.log.read_text()


def test_up_launcher_partial_invalid_input_does_not_disable_other_inputs(up_env, tmp_path, monkeypatch):
    for key in (*PUBLISH_HOST_KEYS, *(item[0] for item in LAUNCHER_FILES.values()), URL_PREFIXES):
        monkeypatch.delenv(key, raising=False)
    config = up_env.home / 'config'
    config.mkdir()
    valid = tmp_path / 'products.yaml'
    valid.write_text('example')
    marker = tmp_path / 'executed'
    (config / 'launcher.env').write_text(
        f'AX_ENVIRONMENTS_HOST_FILE=$(touch {marker})\nAX_PRODUCTS_HOST_FILE={valid}\n'
        f'{URL_PREFIXES}=$(touch {marker})\n')
    result = up_env.run('resources', 'sl-release', 'sl-change', 'sl-scheduled-publisher')
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    bad_fragment = next((up_env.state / 'deploy').glob('launcher-resources-*.compose.yaml'))
    bad = yaml.safe_load(bad_fragment.read_text())['services']['resources']
    assert bad['environment']['AX_ENVIRONMENTS_FILE'] == '/run/ax-config/invalid-input'
    assert 'volumes' not in bad
    good_fragment = next((up_env.state / 'deploy').glob('launcher-sl-release-*.compose.yaml'))
    assert yaml.safe_load(good_fragment.read_text())['services']['sl-release']['volumes'][0]['source'] == str(valid)
    assert not list((up_env.state / 'deploy').glob('launcher-sl-change-*.compose.yaml'))
    url_file = next((up_env.state / 'deploy').glob('launcher-url-*.env'))
    assert url_file.read_text() == f'{URL_PREFIXES}=\n'
    assert (up_env.state / 'hostwatch' / 'registered' / 'sl-change').exists()
    assert str(marker) not in result.stdout + result.stderr


def test_up_launcher_rejects_symlink_and_url_env_overrides_file(up_env, tmp_path, monkeypatch):
    for key in (*PUBLISH_HOST_KEYS, *(item[0] for item in LAUNCHER_FILES.values()), URL_PREFIXES):
        monkeypatch.delenv(key, raising=False)
    config = up_env.home / 'config'
    config.mkdir()
    target = tmp_path / 'target.yaml'
    target.write_text('example')
    link = tmp_path / 'link.yaml'
    link.symlink_to(target)
    (config / 'launcher.env').write_text(
        f'AX_CHANGE_TARGETS_HOST_FILE={link}\n{URL_PREFIXES}=$(invalid)\n')
    url = 'https://example.org/review/'
    result = up_env.run('sl-change', 'sl-scheduled-publisher', extra={URL_PREFIXES: url})
    assert result.returncode == 0, result.stderr
    fragment = next((up_env.state / 'deploy').glob('launcher-sl-change-*.compose.yaml'))
    service = yaml.safe_load(fragment.read_text())['services']['sl-change']
    assert service['environment']['AX_CHANGE_TARGETS_FILE'] == '/run/ax-config/invalid-input'
    assert 'volumes' not in service
    url_file = next((up_env.state / 'deploy').glob('launcher-url-*.env'))
    assert url_file.read_text() == f'{URL_PREFIXES}={url}\n'
    assert not list((up_env.state / 'deploy').glob('launcher-resources-*.compose.yaml'))


def test_up_change_recipes_use_launcher_and_process_override(up_env, tmp_path, monkeypatch):
    key = 'AX_CHANGE_RECIPES_HOST_FILE'
    monkeypatch.delenv(key, raising=False)
    config = up_env.home / 'config'
    config.mkdir()
    source = tmp_path / 'recipes.yaml'
    source.write_text('{}')
    override = tmp_path / 'override-recipes.yaml'
    override.write_text('{}')
    (config / 'launcher.env').write_text(f'{key}="{source}"\n')
    for extra, expected in (({}, source), ({key: str(override)}, override)):
        result = up_env.run('sl-change', extra=extra)
        assert result.returncode == 0, result.stderr
        fragments = sorted((up_env.state / 'deploy').glob('launcher-recipes-*.compose.yaml'),
                           key=lambda path: path.stat().st_mtime_ns)
        fragment = fragments[-1]
        volume = yaml.safe_load(fragment.read_text())['services']['sl-change']['volumes'][0]
        assert volume == {'type': 'bind', 'source': str(expected),
                          'target': '/ax/plugins/subloop/change/verification/recipes.yaml', 'read_only': True}
        assert str(fragment) in up_env.log.read_text()


def test_up_change_recipes_invalid_hides_bundled_and_other_services_start(up_env, tmp_path, monkeypatch):
    key = 'AX_CHANGE_RECIPES_HOST_FILE'
    monkeypatch.delenv(key, raising=False)
    config = up_env.home / 'config'
    config.mkdir()
    marker = tmp_path / 'executed'
    (config / 'launcher.env').write_text(f'{key}=$(touch {marker})\n')
    result = up_env.run('sl-change', 'timeline')
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    fragment = next((up_env.state / 'deploy').glob('launcher-recipes-*.compose.yaml'))
    volume = yaml.safe_load(fragment.read_text())['services']['sl-change']['volumes'][0]
    assert volume['target'] == '/ax/plugins/subloop/change/verification/recipes.yaml'
    assert volume['read_only'] is True
    assert open(volume['source']).read() == ''
    assert (up_env.state / 'hostwatch' / 'registered' / 'timeline').exists()
    assert str(marker) not in result.stdout + result.stderr

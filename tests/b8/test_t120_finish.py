"""T-120 の B8 の仕上げ部分（F-18.6）: host/ax-env.sh、host/ax-up.sh、host/ax-redact-index.sh、host/ax-hostwatch.sh。

B0 の試験（tests/b0/test_t120_host_scripts.py）はこの担当では未実行。統合時に再実行する。
偽の sudo（tests/fakes/b8/fake_sudo.py）だけを使い、実際の sudo・docker・tailscale は呼ばない。
"""

import ipaddress as _fixture_ipaddress
_NET_FIXTURE_3258 = str(_fixture_ipaddress.ip_network("100.64.0.0/10")[3258])
_NET_FIXTURE_5305 = str(_fixture_ipaddress.ip_network("100.64.0.0/10")[5305])
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FAKE_SUDO = ROOT / "tests" / "fakes" / "b8" / "fake_sudo.py"


def sh(script, *args, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "AX_"))}
    e.update(env or {})
    return subprocess.run(["bash", str(ROOT / "host" / script), *args], capture_output=True, text=True, env=e)


@pytest.fixture
def box(tmp_path):
    prof = yaml.safe_load((ROOT / "profile" / "example.yaml").read_text())
    prof["secrets_dir"] = str(tmp_path / "secrets")
    prof["tmp_dir"] = str(tmp_path / "tmp")
    prof["ax_state_dir"] = str(tmp_path / "state")
    (tmp_path / "secrets").mkdir()
    path = tmp_path / "profile.yaml"

    class B:
        pass

    b = B()
    b.tmp, b.prof, b.path, b.state, b.secrets = tmp_path, prof, path, tmp_path / "state", tmp_path / "secrets"
    b.home = tmp_path / "axhome"
    b.home.mkdir()
    b.write = lambda p=None: path.write_text(yaml.safe_dump(p if p is not None else prof, allow_unicode=True))
    b.write()
    return b


def read_env(p):
    return dict(l.split("=", 1) for l in Path(p).read_text().splitlines() if l and not l.startswith("#"))


# ---------------- ax-env.sh ----------------
def test_env_still_builds_the_f186_environment(box):
    out = box.tmp / "p.env"
    r = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out),
           env={"AX_ENV_ID": str(box.tmp / "no-such-id")})
    assert r.returncode == 0, r.stderr
    env = read_env(out)
    assert env["AX_BIND_ADDR"] == "127.0.0.1" and env["AX_HQ_UID"] == "" and env["AX_STATE_DIR"] == str(box.state)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


@pytest.mark.parametrize("addr", ["0.0.0.0", "example.invalid", "::", "256.1.1.1", "  "])
def test_bind_addr_unusable_values_fail_and_write_nothing(box, addr):
    box.prof["bind_addr"] = addr
    box.write()
    out = box.tmp / "p.env"
    r = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out), env={"AX_ENV_TAILSCALE": "echo __NET_FIXTURE_3258__".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258)})
    assert r.returncode == 1 and not out.exists()


def test_bind_addr_tailnet_address_is_accepted_only_if_tailscale_reports_it(box):
    box.prof["bind_addr"] = "__NET_FIXTURE_3258__".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258)
    box.write()
    out = box.tmp / "p.env"
    ok = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out), env={"AX_ENV_TAILSCALE": "echo __NET_FIXTURE_5305__ __NET_FIXTURE_3258__".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258).replace('__NET_FIXTURE_5305__', _NET_FIXTURE_5305)})
    assert ok.returncode == 0, ok.stderr
    assert read_env(out)["AX_BIND_ADDR"] == "__NET_FIXTURE_3258__".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258)
    out2 = box.tmp / "p2.env"
    other = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out2), env={"AX_ENV_TAILSCALE": "echo __NET_FIXTURE_5305__".replace('__NET_FIXTURE_5305__', _NET_FIXTURE_5305)})
    assert other.returncode == 1 and not out2.exists()
    missing = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out2), env={"AX_ENV_TAILSCALE": str(box.tmp / "no-such-tailscale")})
    assert missing.returncode == 1 and not out2.exists()


def test_loopback_does_not_need_tailscale(box):
    r = sh("ax-env.sh", "--profile", str(box.path), "--check", env={"AX_ENV_TAILSCALE": str(box.tmp / "no-such-tailscale")})
    assert r.returncode == 0


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(hq_access="x"),
    lambda p: p["hq_access"].pop("iss"),
    lambda p: p["hq_access"].update(jwks_url=""),
    lambda p: p["hq_access"].update(aud_secret=5),
    lambda p: p.update(gate_port="8909"),
    lambda p: p.update(gate_port=70000),
    lambda p: p.update(env_ssh_targets=[{"id": "a"}]),
    lambda p: p.update(env_ssh_targets="x"),
    lambda p: p.update(archive_dest={"host": "h", "port": "2202", "user": "u"}),
    lambda p: p.update(commander_unit=5),
    lambda p: p.update(sandbox_gate_port=True),
    lambda p: p.update(ntfy_topic=5),
])
def test_profile_structure_and_type_errors_fail_with_1_and_write_nothing(box, mutate):
    mutate(box.prof)
    box.write()
    out = box.tmp / "p.env"
    r = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out))
    assert r.returncode == 1 and not out.exists()


def test_profile_without_optional_hq_access_is_fine(box):
    box.prof.pop("hq_access")
    box.write()
    assert sh("ax-env.sh", "--profile", str(box.path), "--check").returncode == 0


def test_commander_unit_may_be_a_list(box):
    box.prof["commander_unit"] = ["a.service", "b.service"]
    box.write()
    assert sh("ax-env.sh", "--profile", str(box.path), "--check").returncode == 0


def test_ax_hq_uid_and_gid_are_taken_when_the_user_exists(box):
    fake = box.tmp / "fake-id"
    fake.write_text("#!/bin/sh\n[ \"$2\" = ax-hq ] || exit 1\n[ \"$1\" = -u ] && echo 4242 || echo 4343\n")
    fake.chmod(0o755)
    out = box.tmp / "p.env"
    r = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out), env={"AX_ENV_ID": str(fake)})
    assert r.returncode == 0, r.stderr
    env = read_env(out)
    assert env["AX_HQ_UID"] == "4242" and env["AX_HQ_GID"] == "4343"


@pytest.mark.parametrize("id_command", ["false", "missing"])
def test_ax_hq_absent_leaves_empty_values(box, id_command):
    out = box.tmp / "p.env"
    command = str(box.tmp / "no-such-id") if id_command == "missing" else id_command
    r = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out), env={"AX_ENV_ID": command})
    assert r.returncode == 0, r.stderr
    env = read_env(out)
    assert env["AX_HQ_UID"] == "" and env["AX_HQ_GID"] == ""


def test_diff_shows_variable_names_only_never_values(box):
    out = box.tmp / "p.env"
    first = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out))
    assert "追加" in first.stdout and "AX_HOST" in first.stdout
    again = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out))
    assert "差分なし" in again.stdout
    box.prof["host"] = "brand-new-host-name"
    box.prof["ntfy_topic"] = "secretish-topic-value"
    box.write()
    third = sh("ax-env.sh", "--profile", str(box.path), "--out", str(out))
    assert "変更" in third.stdout and "AX_HOST" in third.stdout and "AX_NTFY_TOPIC" in third.stdout
    assert "brand-new-host-name" not in third.stdout + third.stderr and "secretish-topic-value" not in third.stdout + third.stderr
    assert str(box.state) not in third.stdout.replace(str(out), "")


# ---------------- ax-up.sh ----------------
@pytest.fixture
def up(box):
    box.state.mkdir(exist_ok=True)
    (box.state / "deploy").mkdir(exist_ok=True)
    (box.state / "deploy" / "current.env").write_text("AX_TAG=t0\n")
    (box.home / "compose.yaml").write_text("name: ax\n")
    box.sudo_log = box.tmp / "sudo.log"
    box.env = {"AX_HOME": str(box.home), "AX_UP_SUDO": str(FAKE_SUDO), "AX_UP_WAIT_S": "0", "AX_UP_INTERVAL_S": "1",
               "FAKE_SUDO_LOG": str(box.sudo_log), "AX_BIN_DIR": str(box.tmp / "bin")}
    box.up = lambda *a, env=None: sh("ax-up.sh", "--profile", str(box.path), *a, env={**box.env, **(env or {})})
    return box


def hostlog(box):
    p = box.state / "hostwatch.log"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_up_dry_run_prints_commands_and_creates_nothing(box):
    box.env = {"AX_HOME": str(box.home), "AX_UP_SUDO": str(FAKE_SUDO), "FAKE_SUDO_LOG": str(box.tmp / "sudo.log")}
    r = sh("ax-up.sh", "--profile", str(box.path), "--dry-run", "timeline", "ledger", env=box.env)
    assert r.returncode == 0, r.stderr
    assert "docker compose -p ax --project-directory" in r.stdout and "up -d timeline" in r.stdout and "up -d ledger" in r.stdout
    assert "hostwatch/registered/timeline" in r.stdout
    assert not box.state.exists()  # 状態置き場も作らない
    assert not (box.tmp / "sudo.log").exists()  # sudo も呼ばない
    assert list(box.secrets.iterdir()) == []


def test_up_dry_run_with_bad_profile_still_fails(box):
    box.prof.pop("secrets_dir")
    box.write()
    r = sh("ax-up.sh", "--profile", str(box.path), "--dry-run", "timeline", env={"AX_HOME": str(box.home)})
    assert r.returncode == 1


def test_up_refuses_a_concurrent_run_of_the_same_service(up):
    import fcntl

    (up.state / "locks").mkdir(exist_ok=True)
    f = open(up.state / "locks" / "up-timeline.lock", "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = up.up("timeline")
        assert r.returncode == 1 and "実行中" in r.stderr
        assert not up.sudo_log.exists() or "up" not in up.sudo_log.read_text()
        assert not (up.state / "hostwatch" / "registered" / "timeline").exists()
        other = up.up("ledger")  # 別のサービスは通る
        assert other.returncode == 0, other.stderr
    finally:
        f.close()
    assert up.up("timeline").returncode == 0  # ロックが放されれば通る


def test_up_registers_only_after_healthy_and_writes_deploy_up_event(up):
    r = up.up("timeline")
    assert r.returncode == 0, r.stderr
    assert (up.state / "hostwatch" / "registered" / "timeline").exists()
    ev = [e for e in hostlog(up) if e["kind"] == "deploy.up"]
    assert len(ev) == 1 and ev[0]["subject"] == "timeline" and ev[0]["data"]["result"] == "ok"
    assert ev[0]["source"]["component"] == "hostwatch"


def test_up_unhealthy_does_not_register_and_logs_not_healthy(up):
    r = up.up("timeline", env={"FAKE_INSPECT": "unhealthy"})
    assert r.returncode == 1
    assert not (up.state / "hostwatch" / "registered" / "timeline").exists()
    assert [e["data"]["result"] for e in hostlog(up) if e["kind"] == "deploy.up"] == ["not_healthy"]


def test_up_compose_failure_logs_and_fails(up):
    r = up.up("timeline", env={"FAKE_UP_RC": "1"})
    assert r.returncode == 1 and not (up.state / "hostwatch" / "registered" / "timeline").exists()
    assert [e["data"]["result"] for e in hostlog(up) if e["kind"] == "deploy.up"] == ["compose_up_failed"]


def test_up_links_ax_when_host_ax_exists_and_never_overwrites_a_real_file(up):
    (up.home / "host").mkdir()
    (up.home / "host" / "ax").write_text("#!/bin/sh\n")
    assert up.up("timeline").returncode == 0
    link = up.tmp / "bin" / "ax"
    assert link.is_symlink() and os.readlink(link) == str(up.home / "host" / "ax")
    link.rename(up.tmp / "old-ax-link")
    link.write_text("mine")
    r = up.up("ledger")
    assert r.returncode == 0 and link.read_text() == "mine" and "上書きしない" in r.stderr


def test_up_does_not_link_ax_before_b12(up):
    assert up.up("timeline").returncode == 0
    assert not (up.tmp / "bin" / "ax").exists()


# ---------------- ax-redact-index.sh ----------------
def test_index_failure_writes_health_alert_and_keeps_old_index(box):
    box.state.mkdir()
    (box.state / "redact").mkdir()
    (box.state / "redact" / "index").write_text("salt\nhash\n")
    r = sh("ax-redact-index.sh", "--profile", str(box.path), "--secrets-dir", str(box.tmp / "no-such-dir"))
    assert r.returncode == 1
    assert (box.state / "redact" / "index").read_text() == "salt\nhash\n"
    alerts = [e for e in hostlog(box) if e["kind"] == "health.alert"]
    assert len(alerts) == 1 and alerts[0]["data"]["check"] == "redact_index" and alerts[0]["data"]["destination"] == "manager"


def test_index_success_confirms_archive_secrets_are_included(box):
    (box.secrets / "archive").mkdir()
    (box.secrets / "archive" / "old-key").write_text("archived-secret-value-123\n")
    r = sh("ax-redact-index.sh", "--profile", str(box.path))
    assert r.returncode == 0, r.stderr
    assert "archive/ の値はすべて索引にある" in r.stdout
    assert "archived-secret-value-123" not in r.stdout + r.stderr
    assert not (box.state / "hostwatch.log").exists()


def test_indexverify_detects_a_missing_archive_value(box):
    sys.path.insert(0, str(ROOT / "host" / "lib"))
    from hostwatch.indexverify import missing_count

    (box.secrets / "archive").mkdir()
    (box.secrets / "archive" / "k").write_text("archived-secret-value-123\n")
    idx = box.tmp / "index"
    idx.write_text("salt\n" + "0" * 64 + "\n")
    assert missing_count(box.secrets, idx) == 1  # 同じ値は 1 件と数える


def test_up_observer_creates_beat_with_current_env_owner(up):
    (up.state / "deploy" / "current.env").write_text("AX_TAG=t0\nAX_UID=12345\nAX_GID=12346\n")
    r = up.up("observer")
    assert r.returncode == 0, r.stderr
    beat = up.state / "observer.beat"
    assert beat.is_file() and beat.read_bytes() == b""
    assert stat.S_IMODE(beat.stat().st_mode) == 0o644
    calls = [json.loads(x) for x in up.sudo_log.read_text().splitlines()]
    assert ["chown", "12345:12346", str(beat)] in calls


def test_up_observer_preserves_existing_beat(up):
    beat = up.state / "observer.beat"
    beat.write_text("existing heartbeat")
    beat.chmod(0o600)
    before = beat.stat()
    r = up.up("observer")
    assert r.returncode == 0, r.stderr
    after = beat.stat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid) == (before.st_ino, before.st_mode, before.st_uid, before.st_gid)
    assert beat.read_text() == "existing heartbeat"


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_up_observer_rejects_invalid_beat(up, kind):
    beat = up.state / "observer.beat"
    if kind == "directory":
        beat.mkdir()
    else:
        beat.symlink_to(up.tmp / "missing")
    r = up.up("observer")
    assert r.returncode != 0
    assert not up.sudo_log.exists() or not any("up" in json.loads(x) for x in up.sudo_log.read_text().splitlines())


def test_up_other_service_does_not_create_observer_beat(up):
    r = up.up("timeline")
    assert r.returncode == 0, r.stderr
    assert not (up.state / "observer.beat").exists()


def test_up_starts_and_registers_observer_before_other_services(up):
    r = up.up("timeline", "ledger", "observer", "intake")
    assert r.returncode == 0, r.stderr
    calls = [c for c in (json.loads(x) for x in up.sudo_log.read_text().splitlines()) if "up" in c]
    assert [c[-1] for c in calls] == ["observer", "timeline", "ledger", "intake"]
    all_calls = [json.loads(x) for x in up.sudo_log.read_text().splitlines()]
    first_other_up = next(i for i, c in enumerate(all_calls) if c[-3:] == ["up", "-d", "timeline"])
    assert any(c[:2] == ["docker", "inspect"] and c[-1] == "cid-observer" for c in all_calls[:first_other_up])
    reg = sorted((up.state / "hostwatch" / "registered").iterdir(), key=lambda p: p.stat().st_mtime_ns)
    assert reg[0].name == "observer"


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(bind_addr="0.0.0.0"),
    lambda p: p.update(seat_socket="relative.sock"),
    lambda p: p.update(gate_port="8909"),
    lambda p: p.update(env_ssh_targets=[{"id": "a", "host": "test.invalid", "port": True}]),
    lambda p: p.update(archive_dest={"host": "test.invalid", "port": 65536, "user": "u"}),
])
def test_up_invalid_profile_writes_nothing_before_failing(box, mutate):
    mutate(box.prof)
    box.write()
    log = box.tmp / "sudo.log"
    result = sh("ax-up.sh", "--profile", str(box.path), "timeline", env={"AX_HOME": str(box.home), "AX_UP_SUDO": str(FAKE_SUDO), "FAKE_SUDO_LOG": str(log)})
    assert result.returncode == 1
    assert not box.state.exists() and not log.exists() and list(box.secrets.iterdir()) == []


def test_dry_run_validates_the_finished_profile_without_creating_state(box):
    box.prof["gate_port"] = "8909"
    box.write()
    result = sh("ax-up.sh", "--profile", str(box.path), "--dry-run", "timeline", env={"AX_HOME": str(box.home)})
    assert result.returncode == 1 and not box.state.exists()


def test_failed_observer_health_prevents_later_business_service_start(up):
    result = up.up("ledger", "observer", env={"FAKE_INSPECT": "unhealthy"})
    assert result.returncode == 1
    calls = [json.loads(x) for x in up.sudo_log.read_text().splitlines()]
    assert [c[-1] for c in calls if "up" in c] == ["observer"]
    assert not (up.state / "hostwatch/registered/observer").exists()
    assert not (up.state / "hostwatch/registered/ledger").exists()


@pytest.mark.parametrize("service", ["paseo", "paseo-other"])
def test_up_refuses_paseo_before_creating_state_or_invoking_sudo(box, service):
    log = box.tmp / "sudo.log"
    result = sh("ax-up.sh", "--profile", str(box.path), service, env={"AX_HOME": str(box.home), "AX_UP_SUDO": str(FAKE_SUDO), "FAKE_SUDO_LOG": str(log)})
    assert result.returncode == 1 and not box.state.exists() and not log.exists()


def test_timer_index_inputs_are_read_only_and_optional(up):
    history = up.tmp / 'history'
    history.mkdir()
    token = up.tmp / 'embedding-token'
    token.write_text('fixture-credential')
    config = {'AX_CONVERSATION_HISTORY_HOST_DIR': str(history),
              'AX_EMBEDDING_TOKEN_HOST_FILE': str(token), 'AX_CONVERSATION_INDEX_ENABLED': 'true'}
    fragment = up.state / 'deploy' / 'conversation-index.compose.yaml'
    for value in ('', 'TRUE', '1'):
        disabled = dict(config, AX_CONVERSATION_INDEX_ENABLED=value)
        result = up.up('timer-runner', env=disabled)
        assert result.returncode == 0, result.stderr
        assert not fragment.exists()
        assert str(fragment) not in up.sudo_log.read_text()
    result = up.up('timer-runner', env=config)
    assert result.returncode == 0, result.stderr
    service = yaml.safe_load(fragment.read_text())['services']['timer-runner']
    assert service['environment'] == {'AX_CONVERSATION_HISTORY_DIR': '/conversation-history',
                                      'AX_EMBEDDING_TOKEN_FILE': '/run/embedding/token'}
    assert {row['source'] for row in service['volumes']} == {str(history), str(token)}
    assert all(row['read_only'] is True for row in service['volumes'])
    assert 'fixture-credential' not in result.stdout + result.stderr + fragment.read_text()
    assert str(fragment) in up.sudo_log.read_text()
    up.sudo_log.write_text('')
    result = up.up('timer-runner')
    assert result.returncode == 0, result.stderr
    assert str(fragment) not in up.sudo_log.read_text()


def test_hq_start_prepares_key_and_socket_without_rotating_key(up):
    fake = up.tmp / 'fake-id'
    fake.write_text('#!/bin/sh\n[ "$2" = ax-hq ] || exit 1\n[ "$1" = -u ] && echo 4242 || echo 4343\n')
    fake.chmod(0o755)
    args = {'AX_ENV_ID': str(fake)}
    first = up.up('intake', env=args)
    assert first.returncode == 0, first.stderr
    key = up.secrets / 'hq-proof' / 'key'
    saved = key.read_bytes()
    assert len(saved) == 32
    assert stat.S_IMODE(key.stat().st_mode) == 0o640
    second = up.up('intake', env=args)
    assert second.returncode == 0, second.stderr
    calls = [json.loads(line) for line in up.sudo_log.read_text().splitlines()]
    assert calls.count(['chmod', '2710', str(up.state / 'hq-intake')]) == 2
    assert key.read_bytes() == saved
    assert saved.hex() not in first.stdout + first.stderr + second.stdout + second.stderr


def test_other_service_does_not_change_hq_socket_directory(up):
    directory = up.state / 'hq-intake'
    directory.mkdir(mode=0o700)
    before = directory.stat()
    result = up.up('timeline')
    assert result.returncode == 0, result.stderr
    after = directory.stat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid) == (
        before.st_ino, before.st_mode, before.st_uid, before.st_gid)
    calls = [json.loads(line) for line in up.sudo_log.read_text().splitlines()]
    assert not any(call and call[0] in ('chown', 'chmod') and str(directory) in call for call in calls)


def test_security_watch_token_is_registered_without_disclosing_value(up):
    import hashlib
    plugins = up.home / 'plugins'
    plugins.mkdir()
    (plugins / 'registry.yaml').write_text(yaml.safe_dump({'plugins': [
        {'id': 'security-watch', 'kind': 'timer', 'path': 'plugins/timer/security-watch/manifest.yaml'}]}))
    result = up.up('timer-runner')
    assert result.returncode == 0, result.stderr
    token = (up.secrets / 'plugin-tokens' / 'security-watch').read_text().strip()
    rows = (up.state / 'plugin-tokens.sha256').read_text().splitlines()
    assert ['security-watch', hashlib.sha256(token.encode()).hexdigest()] in [row.split() for row in rows]
    assert token not in result.stdout + result.stderr + up.sudo_log.read_text()


def test_release_review_inputs_and_credential_are_provisioned_locally(up):
    import hashlib
    root = up.tmp / 'review-repos'
    root.mkdir()
    paths = {}
    for name in ('repositories', 'hosts', 'terms'):
        path = up.tmp / (name + '.yaml')
        path.write_text('fixture\n')
        paths[name] = path
    settings = {'AX_PUBLISH_REPOSITORIES_HOST_FILE': str(paths['repositories']),
                'AX_PUBLISH_REPOSITORY_HOST_DIR': str(root),
                'AX_PUBLISH_HOSTS_HOST_FILE': str(paths['hosts']),
                'AX_PUBLISH_TERMS_HOST_FILE': str(paths['terms'])}
    result = up.up('sl-release', env=settings)
    assert result.returncode == 0, result.stderr
    fragments = list((up.state / 'deploy').glob('publish-review-*.compose.yaml'))
    assert len(fragments) == 1
    fragment = fragments[0]
    service = yaml.safe_load(fragment.read_text())['services']['sl-release']
    assert all(mount['read_only'] for mount in service['volumes'])
    assert {m['source'] for m in service['volumes']} == {str(root), *(str(p) for p in paths.values())}
    assert service['environment']['AX_PUBLISH_REPOSITORIES_FILE'] == '/run/publish/repositories.yaml'
    token = (up.secrets / 'core-tokens/release-review').read_text().strip()
    table = (up.state / 'core-tokens.sha256').read_text().splitlines()
    assert ['release-review', hashlib.sha256(token.encode()).hexdigest()] in [row.split() for row in table]
    assert token not in result.stdout + result.stderr + up.sudo_log.read_text()
    up.sudo_log.write_text('')
    result = up.up('sl-release')
    assert result.returncode == 0, result.stderr
    assert str(fragment) not in up.sudo_log.read_text()


@pytest.mark.parametrize("invalid", [False, True])
def test_up_with_real_compose_config_and_custom_subnet(up, invalid):
    """Compose config is real; the fake sudo intercepts every Docker operation."""
    (up.home / "compose.yaml").write_text((ROOT / "compose.yaml").read_text())
    (up.home / "compose.d").symlink_to(ROOT / "compose.d", target_is_directory=True)
    (up.home / "docker").symlink_to(ROOT / "docker", target_is_directory=True)
    (up.home / "config").symlink_to(ROOT / "config", target_is_directory=True)
    (up.state / "deploy" / "current.env").write_text(
        "AX_TAG=t0\nAX_CORE_SUBNET=10.41.0.0/24\nAX_CORE_GATEWAY=10.41.0.1\n"
        "AX_FW_ARCHIVER_IP=10.41.0.12\nAX_GATE_IP=10.41.0.13\n")
    current = up.state / "deploy" / "current.env"
    if invalid:
        current.write_text(current.read_text() + "AX_CORE_IP_RANGE=10.41.0.0/25\n")
    before = current.read_text()
    result = up.up("timeline", env={"FAKE_IPAM_REAL": "1"})
    if invalid:
        assert result.returncode != 0 and "ax-ipam:" in result.stderr
        assert current.read_text() == before
        return
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in up.sudo_log.read_text().splitlines()]
    configs = [call for call in calls if "config" in call and "--profile" in call]
    assert len(configs) >= 2
    assert any("/ax-ipam-" in arg and arg.endswith(".compose.yaml") for arg in configs[-1])
    assert "AX_CORE_IP_RANGE" not in (up.state / "deploy" / "current.env").read_text()


@pytest.mark.parametrize("case", ["existing", "matching_range", "range_mismatch", "other_project", "gateway_mismatch", "driver_mismatch", "unavailable", "new", "external"])
def test_up_protects_existing_networks(up, case):
    (up.home / "compose.yaml").write_text((ROOT / "compose.yaml").read_text())
    if case == "external":
        (up.home / "compose.d").mkdir()
        for file in (ROOT / "compose.d").glob("*.yaml"):
            (up.home / "compose.d" / file.name).symlink_to(file)
        (up.home / "compose.d" / "zz-external.yaml").write_text(
            "networks:\n  ax-core:\n    name: outside_shared\n    external: true\n"
            "    driver: !reset null\n    driver_opts: !reset null\n    ipam: !reset null\n")
    else:
        (up.home / "compose.d").symlink_to(ROOT / "compose.d", target_is_directory=True)
    (up.home / "docker").symlink_to(ROOT / "docker", target_is_directory=True)
    (up.home / "config").symlink_to(ROOT / "config", target_is_directory=True)
    current = up.state / "deploy" / "current.env"
    current.write_text("AX_TAG=t0\n" + ("AX_CORE_IP_RANGE=192.0.2.64/26\n" if case in ("range_mismatch", "matching_range") else ""))
    saved = current.read_text()
    networks = [] if case == "new" else [{"Name": "ax_ax-core", "nodedels": {
        "com.docker.compose.project": "other" if case == "other_project" else "ax",
        "com.docker.compose.network": "ax-core"}, "Driver": "overlay" if case == "driver_mismatch" else "bridge", "Internal": False,
        "Attachable": False, "EnableIPv6": False, "Options": {"com.docker.network.bridge.name": "ax-core"},
        "IPAM": {"Driver": "default", "Config": [{
        "Subnet": "192.0.2.0/25", "Gateway": "192.0.2.2" if case == "gateway_mismatch" else "192.0.2.1", **({"IPRange": "192.0.2.64/26"} if case == "matching_range" else {})}]}}]
    if case == "external":
        networks = [{"Name": "outside_shared"}]
    network_file = up.tmp / "networks.json"
    network_file.write_text(json.dumps(networks))
    capture = up.tmp / "compose.json"
    env = {"FAKE_IPAM_REAL": "1", "FAKE_NETWORKS_JSON": str(network_file), "FAKE_COMPOSE_CAPTURE": str(capture)}
    if case == "unavailable":
        env["FAKE_NETWORK_FAILURE"] = "1"
    result = up.up("timeline", env=env)
    calls = [json.loads(line) for line in up.sudo_log.read_text().splitlines()]
    assert current.read_text() == saved
    if case in ("range_mismatch", "other_project", "gateway_mismatch", "driver_mismatch", "unavailable"):
        assert result.returncode != 0 and "ax-ipam:" in result.stderr
        assert not any(call[:2] == ["docker", "compose"] and "up" in call for call in calls)
        return
    assert result.returncode == 0, result.stderr
    core = json.loads(capture.read_text())["networks"]["ax-core"]
    if case in ("existing", "matching_range"):
        assert core["external"] is True and core["name"] == "ax_ax-core"
        assert not core.get("ipam", {}).get("config")
    elif case == "external":
        assert core["external"] is True and core["name"] == "outside_shared"
        assert not any(call[:3] == ["docker", "network", "inspect"] and call[-1] == "outside_shared" for call in calls)
    else:
        assert not core.get("external", False)
        assert core["ipam"]["config"][0]["ip_range"]
    assert not any(call[:3] in (["docker", "network", "rm"], ["docker", "network", "create"]) for call in calls)

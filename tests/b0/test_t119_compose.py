"""T-119 CI の compose 検査（B0）。

`docker compose config` はデーモンに接続しない。ダミーの値（tools/ci-fixtures/）だけで compose.yaml と compose.d/*.yaml を解決できる。
0.0.0.0 の公開、アドレスなしの ports、ports を持つ sl-*、docker.sock の bind を入れると tools/ci.sh が失敗する。ダミーに本物の秘密は入れない。
docker の CLI が無い環境では、CLI を使う試験だけ飛ばす（compose_check と ci.env の試験は動く）。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from axcommon.profile import MAPPING
from tests.b0.helpers import load_tool

compose_check = load_tool("compose_check")

HAS_DOCKER = shutil.which("docker") is not None
needs_docker = pytest.mark.skipif(not HAS_DOCKER, reason="docker の CLI が無い（ax-ci イメージには入っている）")


def _env(extra=None):
    e = {k: v for k, v in os.environ.items()}
    e.update(extra or {})
    return e


def run_ci(repo_root, compose_dir, steps="3", extra_env=None):
    env = _env({"AX_CI_STEPS": steps, "AX_CI_COMPOSE_DIR": str(compose_dir), **(extra_env or {})})
    return subprocess.run(["bash", str(repo_root / "tools" / "ci.sh")], capture_output=True, text=True, env=env, cwd=str(repo_root))


@pytest.fixture
def compose_copy(repo_root, tmp_path):
    """compose.yaml、compose.d/、docker/ の写し。壊れた compose.d/*.yaml を足して ci.sh の失敗を見る。"""
    d = tmp_path / "proj"
    shutil.copytree(repo_root / "compose.d", d / "compose.d")
    shutil.copytree(repo_root / "docker", d / "docker")
    shutil.copy(repo_root / "compose.yaml", d / "compose.yaml")
    return d


def _write(d, name, text):
    (d / "compose.d" / name).write_text(text, encoding="utf-8")


def _svc(extra_yaml):
    return "services:\n  " + extra_yaml.replace("\n", "\n  ") + "\n"


# ---- ci.env とダミーの秘密（デーモン不要）----
def test_ci_env_has_every_ax_variable_of_the_f186_table(ci_env):
    expected = {var for _k, var, _t in MAPPING} | {"AX_HOME", "AX_UID", "AX_GID", "AX_HQ_UID", "AX_HQ_GID", "AX_TAG"}
    assert expected <= set(ci_env)


def test_ci_env_bind_addr_is_loopback_and_tag_is_ci(ci_env):
    assert ci_env["AX_BIND_ADDR"] == "127.0.0.1" and ci_env["AX_TAG"] == "ci"


def test_ci_env_paths_are_relative_under_tools_ci_fixtures(ci_env):
    for k in ("AX_SECRETS_DIR", "AX_STATE_DIR", "AX_TMP_DIR"):
        assert ci_env[k].startswith("./tools/ci-fixtures/")


def test_fixture_secrets_are_all_the_dummy_word(repo_root):
    files = [p for p in (repo_root / "tools" / "ci-fixtures" / "secrets").rglob("*") if p.is_file() and p.name != ".gitkeep"]
    assert len(files) >= 5
    assert all(p.read_text().strip() == "dummy" for p in files)


def test_ci_fixtures_hold_no_real_looking_values(repo_root):
    pat = [r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", r"\b[0-9a-f]{32,}\b", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", r"\bsk-[A-Za-z0-9_-]{20,}", r"ghp_"]
    for p in (repo_root / "tools" / "ci-fixtures").rglob("*"):
        if p.is_file():
            text = p.read_text(errors="ignore")
            for rx in pat:
                assert not re.search(rx, text), (p.name, rx)


def test_every_secret_declared_in_compose_has_a_dummy_file(repo_root):
    import yaml

    doc = yaml.safe_load((repo_root / "compose.yaml").read_text())
    for name, spec in doc["secrets"].items():
        rel = spec["file"].replace("${AX_SECRETS_DIR}", "tools/ci-fixtures/secrets")
        assert (repo_root / rel).is_file(), name


# ---- compose_check の判定（YAML を読まない。JSON だけ）----
def _model(**services):
    return {"services": services}


def test_check_accepts_gate_on_loopback_ax_core_only():
    m = _model(gate={"ports": [{"host_ip": "127.0.0.1", "published": "8909", "target": 8909}], "networks": {"ax-core": None}})
    assert compose_check.check(m) == []


@pytest.mark.parametrize("ip", ["0.0.0.0", "::", "", None])
def test_check_rejects_ports_without_a_real_bind_address(ip):
    port = {"published": "8909", "target": 8909}
    if ip is not None:
        port["host_ip"] = ip
    bad = compose_check.check(_model(gate={"ports": [port], "networks": {"ax-core": None}}))
    assert bad and "host_ip" in bad[0]


def test_check_rejects_ports_in_sl_services_even_on_loopback():
    bad = compose_check.check(_model(**{"sl-issue-automation": {"ports": [{"host_ip": "127.0.0.1", "published": "9000", "target": 8080}]}}))
    assert any("サブループ" in b for b in bad)


def test_check_rejects_ports_in_services_other_than_gate():
    bad = compose_check.check(_model(timeline={"ports": [{"host_ip": "127.0.0.1", "published": "8080", "target": 8080}]}))
    assert any("許可していない" in b for b in bad)
    assert compose_check.check(_model(timeline={"ports": [{"host_ip": "127.0.0.1", "published": "8080", "target": 8080}]}), allow_ports={"timeline"}) == []


@pytest.mark.parametrize("src", ["/var/run/docker.sock", "/run/docker.sock"])
def test_check_rejects_docker_socket_volumes(src):
    bad = compose_check.check(_model(x={"volumes": [{"type": "bind", "source": src, "target": "/s"}]}))
    assert any("docker.sock" in b for b in bad)


def test_check_rejects_gate_on_other_networks():
    bad = compose_check.check(_model(gate={"networks": {"ax-core": None, "ax-work": None}}))
    assert any("ax-core だけ" in b for b in bad)


def test_check_network_layout_matches_f181():
    good = {"ax-core": {"driver_opts": {"com.docker.network.bridge.name": "ax-core"}, "ipam": {"config": [{"subnet": "192.0.2.0/25"}]}},
            "ax-job": {"internal": True, "driver_opts": {"com.docker.network.bridge.name": "ax-job"}, "ipam": {"config": [{"subnet": "203.0.113.0/25"}]}}}
    assert compose_check.check({"services": {}, "networks": good}) == []
    bad = json.loads(json.dumps(good))
    bad["ax-job"]["internal"] = False
    bad["ax-core"]["ipam"]["config"][0]["subnet"] = "192.0.2.0/24"
    out = compose_check.check({"services": {}, "networks": bad})
    assert any("ax-job" in b and "internal" in b for b in out) and any("ax-core" in b and "subnet" in b for b in out)


def test_compose_check_cli_exit_codes(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps(_model(x={"ports": [{"host_ip": "0.0.0.0", "published": "1"}]})))
    assert compose_check.main(["compose_check", str(f)]) == 1
    f.write_text(json.dumps(_model()))
    assert compose_check.main(["compose_check", str(f)]) == 0
    assert compose_check.main(["compose_check", str(tmp_path / "none.json")]) == 2


# ---- docker compose config（デーモンに接続しない）----
@needs_docker
def test_repo_compose_resolves_with_dummies_and_passes_ci_step3(repo_root):
    r = run_ci(repo_root, repo_root)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "variable is not set" not in r.stderr


@needs_docker
def test_ci_step3_works_even_if_docker_host_points_nowhere(repo_root):
    r = run_ci(repo_root, repo_root, extra_env={"DOCKER_HOST": "tcp://203.0.113.1:2375"})
    assert r.returncode == 0, r.stderr  # ci.sh が DOCKER_HOST を存在しない先に固定する。外へ接続しない


@needs_docker
def test_ci_step3_fails_on_0_0_0_0_publish(repo_root, compose_copy):
    _write(compose_copy, "bad.yaml", _svc("gate:\n  image: x\n  networks: [ax-core]\n  ports: [\"0.0.0.0:8909:8909\"]"))
    r = run_ci(repo_root, compose_copy)
    assert r.returncode != 0 and "host_ip" in r.stdout


@needs_docker
def test_ci_step3_fails_on_ports_without_address(repo_root, compose_copy):
    _write(compose_copy, "bad.yaml", _svc("gate:\n  image: x\n  networks: [ax-core]\n  ports: [\"8909:8909\"]"))
    assert run_ci(repo_root, compose_copy).returncode != 0


@needs_docker
def test_ci_step3_fails_on_ports_in_compose_d_sl_file(repo_root, compose_copy):
    _write(compose_copy, "sl-demo.yaml", _svc("sl-demo:\n  image: x\n  networks: [ax-work]\n  ports: [\"${AX_BIND_ADDR}:9000:8080\"]"))
    r = run_ci(repo_root, compose_copy)
    assert r.returncode != 0 and "サブループ" in r.stdout


@needs_docker
def test_ci_step3_fails_on_docker_socket_bind(repo_root, compose_copy):
    _write(compose_copy, "bad.yaml", _svc("x:\n  image: x\n  volumes: [\"/var/run/docker.sock:/var/run/docker.sock\"]"))
    r = run_ci(repo_root, compose_copy)
    assert r.returncode != 0 and "docker.sock" in r.stdout


@needs_docker
def test_ci_step3_fails_on_variable_missing_from_ci_env(repo_root, compose_copy):
    _write(compose_copy, "bad.yaml", _svc("x:\n  image: ${AX_NOT_IN_CI_ENV}"))
    r = run_ci(repo_root, compose_copy)
    assert r.returncode != 0 and "variable is not set" in r.stderr


@needs_docker
def test_ci_step3_accepts_gate_on_loopback_in_ax_core(repo_root, compose_copy):
    _write(compose_copy, "ok.yaml", _svc("gate:\n  image: x\n  networks: [ax-core]\n  ports: [\"${AX_BIND_ADDR}:${AX_GATE_PORT}:8909\"]"))
    r = run_ci(repo_root, compose_copy)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_docker
def test_ci_step3_fails_when_a_compose_file_is_broken(repo_root, compose_copy):
    _write(compose_copy, "bad.yaml", "services: [this is not a mapping\n")
    assert run_ci(repo_root, compose_copy).returncode != 0


@needs_docker
def test_b0_service_extends_base_with_tmpdir_and_hdd_bind(repo_root):
    env = _env({"DOCKER_HOST": "unix:///nonexistent"})
    r = subprocess.run(["docker", "compose", "-p", "ax-ci", "--project-directory", str(repo_root), "--env-file", str(repo_root / "tools/ci-fixtures/ci.env"),
                        "-f", str(repo_root / "compose.yaml"), "-f", str(repo_root / "compose.d/b0.yaml"), "--profile", "*", "config", "--format", "json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    svc = json.loads(r.stdout)["services"]["axcommon-selfcheck"]
    assert svc["environment"]["TMPDIR"] == "/tmp-hdd"
    assert any(v.get("target") == "/tmp-hdd" for v in svc["volumes"])
    assert "ports" not in svc and svc["cap_drop"] == ["ALL"]

@needs_docker
def test_compose_uses_configured_networks_and_service_addresses(repo_root):
    env = _env({
        "AX_CORE_SUBNET": "203.0.113.0/26",
        "AX_CORE_GATEWAY": "203.0.113.1",
        "AX_FW_ARCHIVER_IP": "203.0.113.12",
        "AX_WORK_SUBNET": "198.51.100.128/26",
        "AX_WORK_GATEWAY": "198.51.100.129",
        "AX_FW_RESOURCES_IP": "198.51.100.139",
        "AX_FW_WORKER_POOL_IP": "198.51.100.138",
        "AX_EGRESS_PROXY_IP": "198.51.100.130",
    })
    cmd = ["docker", "compose", "--project-directory", str(repo_root),
           "--env-file", str(repo_root / "tools/ci-fixtures/ci.env")]
    for name in ("compose.yaml", "compose.d/b1.yaml", "compose.d/b6a.yaml", "compose.d/b6b.yaml"):
        cmd += ["-f", str(repo_root / name)]
    result = subprocess.run(cmd + ["config", "--format", "json"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    assert model["networks"]["ax-core"]["ipam"]["config"][0]["subnet"] == env["AX_CORE_SUBNET"]
    assert model["services"]["archiver"]["networks"]["ax-core"]["ipv4_address"] == env["AX_FW_ARCHIVER_IP"]
    assert model["services"]["resources"]["environment"]["AX_WORK_SUBNET"] == env["AX_WORK_SUBNET"]
    assert model["services"]["egress-proxy"]["environment"]["EGRESS_GATE_HOST"] == env["AX_FW_RESOURCES_IP"]
    assert model["services"]["worker-pool"]["networks"]["ax-work"]["ipv4_address"] == env["AX_FW_WORKER_POOL_IP"]

"""compose 関連の残りの欠落件を埋める試験（compose-profile）。

- 22 件の秘密（全束。compose.yaml の 8 件だけでは不足していた）が揃い、値はいずれも dummy
- bind のホスト側の相手（状態置き場・秘密置き場のもと）は tools/ci-fixtures/ の下で存在する
- 例の profile（profile/example.yaml）と雛形（templates/profile/profile.template.yaml）が
  axcommon.profile と envfinish の検査を通る
- gate の待ち受けは ${AX_BIND_ADDR}:${AX_GATE_PORT}:8080 の tcp で、アドレスは 127.0.0.1
  （0.0.0.0 やアドレスなしは不可）で、網は ax-core だけ。他のサービスは ports を持たない
"""
import ipaddress
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from axcommon.profile import REQUIRED, build_env, MAPPING
from tests.b0.helpers import load_tool

compose_check = load_tool("compose_check")

HAS_DOCKER = shutil.which("docker") is not None
needs_docker = pytest.mark.skipif(not HAS_DOCKER, reason="docker の CLI が無い")


def _load_compose_files(repo_root):
    files = [repo_root / "compose.yaml"]
    files += sorted((repo_root / "compose.d").glob("*.yaml"))
    return files


@needs_docker
def test_full_compose_render_and_gate_ports_rule(repo_root):
    env = dict(os.environ)
    env["DOCKER_HOST"] = "unix:///nonexistent"
    files = _load_compose_files(repo_root)
    args = ["docker", "compose", "-p", "ax-ci", "--project-directory", str(repo_root),
            "--env-file", str(repo_root / "tools/ci-fixtures/ci.env")]
    for f in files:
        args += ["-f", str(f)]
    args += ["--profile", "*", "config", "--format", "json"]
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "variable is not set" not in r.stderr
    model = json.loads(r.stdout)
    assert compose_check.check(model, frozenset({"gate"}), True) == []

    gate = model["services"]["gate"]
    assert [(p.get("host_ip"), int(p["published"]), int(p["target"])) for p in gate["ports"]] == \
        [("127.0.0.1", 8909, 8080)]
    assert set(gate["networks"]) == {"ax-core"}
    for name, svc in model["services"].items():
        assert not svc.get("ports") or name == "gate", name
    for name, svc in model["services"].items():
        for v in svc.get("volumes") or []:
            src = v.get("source", "")
            assert src not in ("/var/run/docker.sock", "/run/docker.sock"), name


def test_every_compose_secret_has_a_dummy_fixture(repo_root):
    declared = {}
    for f in _load_compose_files(repo_root):
        doc = yaml.safe_load(f.read_text())
        if not doc:
            continue
        for name, spec in (doc.get("secrets") or {}).items():
            declared[name] = spec["file"].replace("${AX_SECRETS_DIR}",
                                                   "tools/ci-fixtures/secrets")
    assert len(declared) == 36, sorted(declared)
    for name, rel in sorted(declared.items()):
        path = repo_root / rel
        assert path.is_file(), name
        assert path.read_text().strip() == "dummy", name


@needs_docker
def test_fixture_binds_resolve_inside_ci_fixtures(repo_root):
    """ci.env の *_DIR が tools/ci-fixtures/ の下を指すため、合成済みの bind 源は
    tools/ci-fixtures/ の内側か docker の名前付きボリュームだけでなければならない。"""
    env = dict(os.environ)
    env["DOCKER_HOST"] = "unix:///nonexistent"
    args = ["docker", "compose", "-p", "ax-ci", "--project-directory", str(repo_root),
            "--env-file", str(repo_root / "tools/ci-fixtures/ci.env")]
    for f in _load_compose_files(repo_root):
        args += ["-f", str(f)]
    args += ["--profile", "*", "config", "--format", "json"]
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    model = json.loads(r.stdout)
    fixture_root = (repo_root / "tools/ci-fixtures").resolve()
    for name, svc in model["services"].items():
        for v in svc.get("volumes") or []:
            if v.get("type") != "bind":
                continue
            src = Path(v.get("source", ""))
            if src.is_absolute() and src.resolve().is_relative_to(fixture_root):
                continue
            # ${AX_HOME} からの bind（config 等）と、その兄弟（work 等）であってよい。
            # それは repo の親（work/ の一級）まで許し、それより外は不可。
            assert src.is_absolute() and src.resolve().is_relative_to(repo_root.parent), (name, src)
            assert "/home/" not in str(src.resolve()) or str(src.resolve()).startswith(str(Path.home()) + "/.claude/"), (name, src)
    # 壊れ表記の検出: compose 定義に fixture の実パスを直書きしない
    for f in _load_compose_files(repo_root):
        assert "ci-fixtures" not in f.read_text(), f.name


def test_hq_access_allowed_users_file_matches_compose_secret(repo_root):
    """HQ は profile の hq_access.allowed_users_file と config/hq-allowed-users.yaml の
    file が混ざると users_filename_mismatch で起動しない。例と雛形は compose の秘密
    hq-allowed-users と一致しなければならない（書かれていた hq-access-users は無）"""
    for rel in ("profile/example.yaml", "templates/profile/profile.template.yaml"):
        raw = yaml.safe_load((repo_root / rel).read_text())
        assert raw["hq_access"]["allowed_users_file"] == "hq-allowed-users", rel
    for f in _load_compose_files(repo_root):
        assert "hq-access-users" not in f.read_text(), f.name
    # secrets の表にも hq-allowed-users が載る
    assert "hq-allowed-users" in (repo_root / "templates/secrets/README.md").read_text()


def test_no_committed_hub_host_profile(repo_root):
    """実環境の hub-host プロファイルは git に置かない。リポジトリ内に雛形が這うと
    実値の commit が混ざる。存在しないことを固定する。"""
    assert not (repo_root / "profile/hub-host.yaml").exists()
    assert not (repo_root / "templates/profile/hub-host.yaml").exists()


def test_example_and_template_profiles_pass_validation(repo_root):
    for rel in ("profile/example.yaml", "templates/profile/profile.template.yaml"):
        path = repo_root / rel
        raw = yaml.safe_load(path.read_text())
        assert isinstance(raw, dict), rel
        for k in REQUIRED:
            assert raw.get(k) not in (None, ""), (rel, k)
        env = build_env(raw, environ={"PATH": os.environ["PATH"]})
        assert env["AX_BIND_ADDR"] == "127.0.0.1"
        if "gate_port" in raw and raw["gate_port"] not in (None, ""):
            assert int(raw["gate_port"]) == 8909
        # envfinish の型の検査を通る
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "envfinish", repo_root / "host/lib/hostwatch/envfinish.py")
        ef = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ef)
        ef.check_types(raw)
        ef.check_bind_addr(str(raw["bind_addr"]))


def test_gate_ports_string_is_bind_addr_only(repo_root):
    text = (repo_root / "compose.d" / "b12b1.yaml").read_text()
    assert '"${AX_BIND_ADDR}:${AX_GATE_PORT}:8080"' in text
    assert "0.0.0.0" not in text
    assert '"8909:8080"' not in text
    dir_texts = "\n".join(p.read_text() for p in (repo_root / "compose.d").glob("*.yaml"))
    assert "ports:" in dir_texts
    for line in dir_texts.splitlines():
        assert "0.0.0.0" not in line, line

@pytest.mark.realenv
@pytest.mark.skipif(os.environ.get("AX_TEST_DATA_VOLUME") != "1", reason="requires an isolated Docker daemon test")
def test_resources_volume_initialization_e2e(repo_root, tmp_path):
    """Exercise Compose initialization and write as the configured service user."""
    import uuid

    suffix = uuid.uuid4().hex[:12]
    project = f"ax-datavol-test-{suffix}"
    volume = f"{project}-resources-data"
    override = tmp_path / "override.yaml"
    override.write_text(yaml.safe_dump({"volumes": {
        "resources-data": {"name": volume},
        "worker-pool-data": {"name": f"{project}-worker-pool-data"},
    }}))
    env = dict(os.environ, AX_UID="12345", AX_GID="12346", AX_TAG="int3-trial")
    args = ["docker", "compose", "-p", project, "--project-directory", str(repo_root),
            "--env-file", str(repo_root / "tools/ci-fixtures/ci.env"),
            "-f", str(repo_root / "compose.yaml"), "-f", str(repo_root / "compose.d/b6a.yaml"),
            "-f", str(override)]
    try:
        config = subprocess.run(args + ["config", "--format", "json"], env=env, capture_output=True, text=True, check=True)
        model = json.loads(config.stdout)
        assert model["volumes"]["resources-data"]["name"] == volume
        assert model["services"]["resources"]["user"] == "12345:12346"
        assert model["services"]["resources"]["depends_on"]["resources-volume-init"]["condition"] == "service_completed_successfully"
        subprocess.run(args + ["run", "--rm", "--no-deps", "resources-volume-init"], env=env, check=True)
        subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "12345:12346",
                        "--mount", f"type=volume,source={volume},target=/data",
                        "--entrypoint", "sh", "ax/core:int3-trial", "-c",
                        "test $(stat -c %u:%g /data) = 12345:12346 && echo test > /data/write-test"],
                       env=env, check=True)
    finally:
        subprocess.run(args + ["down", "--remove-orphans"], env=env, check=True)


@needs_docker
@pytest.mark.parametrize("custom", [False, True])
def test_all_profiles_dynamic_ranges_exclude_fixed_addresses(repo_root, custom):
    env = dict(os.environ, DOCKER_HOST="unix:///nonexistent")
    values = {
        "AX_CORE_SUBNET": "10.201.10.0/24", "AX_CORE_GATEWAY": "10.201.10.1",
        "AX_WORK_SUBNET": "10.201.20.0/24", "AX_WORK_GATEWAY": "10.201.20.1",
        "AX_JOB_SUBNET": "10.201.30.0/24", "AX_JOB_GATEWAY": "10.201.30.1",
        "AX_PUBLISH_REVIEW_SUBNET": "10.201.40.0/24", "AX_PUBLISH_REVIEW_GATEWAY": "10.201.40.1",
        "AX_FW_ARCHIVER_IP": "10.201.10.12", "AX_GATE_IP": "10.201.10.13",
        "AX_FW_RESOURCES_IP": "10.201.20.11", "AX_FW_WORKER_POOL_IP": "10.201.20.10",
        "AX_EGRESS_PROXY_IP": "10.201.20.2", "AX_PUBLISH_REVIEW_RESOURCES_IP": "10.201.40.11",
    }
    if custom:
        env.update(values)
    else:
        for key in values:
            env.pop(key, None)
    cmd = ["python3", "-P", str(repo_root / "host/lib/hostwatch/ipam_compose.py"),
           "docker", "compose", "-p", "ax-ci", "--project-directory", str(repo_root),
           "--env-file", str(repo_root / "tools/ci-fixtures/ci.env")]
    for file in _load_compose_files(repo_root):
        cmd += ["-f", str(file)]
    result = subprocess.run(cmd + ["--", "--profile", "*", "config", "--format", "json"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    networks = {}
    for name in ("ax-core", "ax-work", "ax-job", "ax-publish-review"):
        item = model["networks"][name]["ipam"]["config"][0]
        subnet = ipaddress.ip_network(item["subnet"])
        pool = ipaddress.ip_network(item["ip_range"])
        assert pool.subnet_of(subnet), name
        assert ipaddress.ip_address(item["gateway"]) not in pool, name
        networks[name] = (subnet, pool)
    allocated = set()
    for service, spec in model["services"].items():
        for network, cfg in (spec.get("networks") or {}).items():
            addr = (cfg or {}).get("ipv4_address")
            if addr and network in networks:
                ip = ipaddress.ip_address(addr)
                subnet, pool = networks[network]
                assert ip in subnet and ip not in pool, (service, network)
                assert (network, ip) not in allocated, (service, network)
                allocated.add((network, ip))


@needs_docker
@pytest.mark.parametrize("case", ["fixed", "precedence", "invalid", "exhausted"])
def test_ipam_public_compose_command(repo_root, tmp_path, case):
    """Exercise the actual Compose interpolation and the generated overlay."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AX_")}
    env["DOCKER_HOST"] = "unix:///nonexistent"
    overrides = tmp_path / "current.env"
    overrides.write_text("")
    overlay = tmp_path / "fixed.yaml"
    if case == "fixed":
        overlay.write_text("services:\n  inspector:\n    networks:\n      ax-core:\n        ipv4_address: 192.0.2.100\n")
    elif case == "precedence":
        overrides.write_text("AX_CORE_SUBNET=10.40.0.0/24\nAX_CORE_GATEWAY=10.40.0.1\nAX_FW_ARCHIVER_IP=10.40.0.12\nAX_GATE_IP=10.40.0.13\n")
        env.update(AX_CORE_SUBNET="10.41.0.0/24", AX_CORE_GATEWAY="10.41.0.1",
                   AX_FW_ARCHIVER_IP="10.41.0.12", AX_GATE_IP="10.41.0.13")
        overlay.write_text("{}\n")
    elif case == "invalid":
        overrides.write_text("AX_CORE_IP_RANGE=192.0.2.0/26\n")
        overlay.write_text("{}\n")
    else:
        overrides.write_text("AX_CORE_SUBNET=10.42.0.0/29\nAX_CORE_GATEWAY=10.42.0.1\nAX_FW_ARCHIVER_IP=10.42.0.2\nAX_GATE_IP=10.42.0.3\n")
        overlay.write_text("services:\n  inspector:\n    networks:\n      ax-core:\n        ipv4_address: 10.42.0.4\n  observer:\n    networks:\n      ax-core:\n        ipv4_address: 10.42.0.5\n  emergency:\n    networks:\n      ax-core:\n        ipv4_address: 10.42.0.6\n")
    cmd = ["python3", "-P", str(repo_root / "host/lib/hostwatch/ipam_compose.py"),
           "docker", "compose", "-p", "ax-ci", "--project-directory", str(repo_root),
           "--env-file", str(repo_root / "tools/ci-fixtures/ci.env"), "--env-file", str(overrides)]
    for file in [*_load_compose_files(repo_root), overlay]:
        cmd += ["-f", str(file)]
    result = subprocess.run(cmd + ["--", "--profile", "*", "config", "--format", "json"],
                            env=env, capture_output=True, text=True)
    if case in {"invalid", "exhausted"}:
        assert result.returncode != 0
        assert "ax-ipam:" in result.stderr
        if case == "exhausted":
            assert "確保できません" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    core = json.loads(result.stdout)["networks"]["ax-core"]["ipam"]["config"][0]
    if case == "fixed":
        assert ipaddress.ip_address("192.0.2.100") not in ipaddress.ip_network(core["ip_range"])
    else:
        assert core["subnet"] == "10.41.0.0/24"
        assert ipaddress.ip_network(core["ip_range"]).subnet_of(ipaddress.ip_network(core["subnet"]))

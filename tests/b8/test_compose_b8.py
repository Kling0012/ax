"""compose.d/b8.yaml の observer（F-18.4、D-14、D-10.1）。`docker compose config` はデーモンに接続しない（DOCKER_HOST を存在しない先に固定）。"""
import json
from functools import lru_cache
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker の CLI が無い（compose config を使う試験だけ飛ばす）")


@lru_cache(maxsize=1)
def resolved():
    env = {**os.environ, "DOCKER_HOST": "unix:///nonexistent"}
    files = ["-f", str(ROOT / "compose.yaml")] + [a for f in [ROOT / "compose.d" / name for name in ("b1.yaml", "b8.yaml")] for a in ("-f", str(f))]
    r = subprocess.run(["docker", "compose", "-p", "ax-ci", "--project-directory", str(ROOT), "--env-file", str(ROOT / "tools/ci-fixtures/ci.env"),
                        *files, "--profile", "*", "config", "--format", "json"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "variable is not set" not in r.stderr
    return json.loads(r.stdout)


@needs_docker
def test_observer_service_shape():
    svc = resolved()["services"]["observer"]
    assert list(svc["networks"]) == ["ax-core"]  # 中核の網だけ（F-18.4）
    assert not svc.get("ports")
    assert svc["command"] == ["python", "-m", "core.observer"]
    assert svc["user"] == "1000:1000"
    assert svc.get("cap_drop") == ["ALL"] and "no-new-privileges:true" in svc.get("security_opt", [])
    assert svc["restart"] == "unless-stopped"
    assert svc["environment"]["AX_STATE_DIR"] == "/state" and svc["environment"]["AX_OBSERVER_DATA"] == "/data"
    assert svc["healthcheck"]["test"][-1] == "axcommon.healthcheck"


@needs_docker
def test_observer_waits_for_initialized_data_volume():
    services = resolved()["services"]
    assert services["observer"]["depends_on"]["archive-init"]["condition"] == "service_completed_successfully"
    mounts = {v["target"]: v for v in services["archive-init"]["volumes"]}
    assert mounts["/v/observer-data"]["source"] == "observer-data"


@needs_docker
def test_observer_mounts_state_and_cutover_read_only_and_no_docker_socket():
    svc = resolved()["services"]["observer"]
    mounts = {v["target"]: v for v in svc["volumes"]}
    assert mounts["/state"]["type"] == "bind" and mounts["/state"].get("read_only") is True
    assert mounts["/state/host-requests"]["type"] == "bind" and not mounts["/state/host-requests"].get("read_only")
    assert not mounts["/state/observer.beat"].get("read_only")
    assert mounts["/state/observer.beat"]["bind"].get("create_host_path", False) is False
    assert mounts["/state/cutover"]["read_only"] is True  # 切り替えの記録は読み取り専用（D-10.1）
    assert mounts["/data"]["type"] == "volume"
    assert not any("docker.sock" in str(v.get("source", "")) for v in svc["volumes"])
    assert "privileged" not in svc or not svc["privileged"]


@needs_docker
def test_only_observer_and_emergency_bind_the_whole_state_dir():
    """S-11.6: /state を開けるコンテナは observer と emergency の経路に限る。intake は host-requests/ だけ（他の束の違反は OPEN.md に記録する）。"""
    model = resolved()
    whole = [name for name, svc in model["services"].items()
             if any(v.get("target") == "/state" and v.get("type") == "bind" for v in svc.get("volumes", []))]
    assert "observer" in whole
    extra = sorted(set(whole) - {"observer", "emergency"})
    assert extra == []
    assert any(x["source"] == "core-token-observer" for x in model["services"]["observer"]["secrets"])

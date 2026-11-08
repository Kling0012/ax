"""Resolve b10a with CI values and a clearly isolated stub for the unfinished HQ base."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_compose_with_ci_values_and_dummy_hq(repo_root, tmp_path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker compose config unavailable; no packages installed")
    stub = tmp_path / "unfinished-hq-base.yaml"
    stub.write_text("services:\n  hq:\n    image: ax/core:ci\n    networks: [ax-core]\n")
    files = [repo_root / "compose.yaml"] + [repo_root / "compose.d" / f"{pkg}.yaml"
            for pkg in ("b0", "b1", "b5", "b8", "b10a")] + [stub]
    command = [docker, "compose", "--project-directory", str(repo_root), "-p", "ax-ci",
               "--env-file", str(repo_root / "tools/ci-fixtures/ci.env")]
    for path in files:
        command.extend(["-f", str(path)])
    command.extend(["config", "--format", "json"])
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AX_", "COMPOSE_"))}
    env["DOCKER_HOST"] = "unix://" + str(tmp_path / "no-daemon.sock")
    result = subprocess.run(command, cwd=repo_root, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "variable is not set" not in result.stderr
    services = json.loads(result.stdout)["services"]
    emergency = services["emergency"]
    assert set(emergency["networks"]) == {"ax-core"}
    assert not emergency.get("ports")
    assert not emergency.get("privileged")
    mounts = {mount["target"]: mount for mount in emergency["volumes"]}
    assert mounts["/state"]["read_only"] is True
    assert mounts["/state/host-requests"].get("read_only", False) is False
    assert mounts["/data"]["source"] == "emergency-data"
    assert set(mounts).isdisjoint({"/run/docker.sock", "/var/run/docker.sock"})
    assert {s["source"] for s in emergency["secrets"]} >= {"control-token-emergency", "pii-list"}
    assert {s["source"] for s in services["intake"]["secrets"]} >= {"core-token-intake"}
    assert {s["source"] for s in services["observer"]["secrets"]} >= {"core-token-observer", "control-token-" + "backup"}
    hq_env = services["hq"]["environment"]
    assert hq_env["AX_NTFY_TOPIC"] == "ci"
    assert hq_env["AX_NTFY_ENDPOINT"] == "http://ntfy.ci.invalid:8908"
    assert hq_env["AX_HQ_NOTIFY_RUN"] == "1"
    assert any(v["source"] == "emergency-data" for v in services["archive-init"]["volumes"])
    assert any(v["source"] == "emergency-data" and v["volume"]["subpath"] == "archive"
               for v in services["archiver"]["volumes"])

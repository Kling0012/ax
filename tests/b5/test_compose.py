"""compose.d/b5.yaml の検査（F-18.4、F-9.2、D-14。T-6 の「内部コンテナが入口に届かない」の compose 側）。

`docker compose config` はデーモンに接続しない。ダミーの値（tools/ci-fixtures/）だけで解決できる。
docker の CLI が無い環境では、CLI を使う試験だけ飛ばす。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests.b0.helpers import load_tool

compose_check = load_tool("compose_check")
HAS_DOCKER = shutil.which("docker") is not None
needs_docker = pytest.mark.skipif(not HAS_DOCKER, reason="docker の CLI が無い")


def _config(repo_root):
    env = dict(os.environ)
    env["DOCKER_HOST"] = "unix:///nonexistent"  # デーモンに接続しない
    r = subprocess.run(
        ["docker", "compose", "-p", "ax-ci", "--project-directory", str(repo_root),
         "--env-file", str(repo_root / "tools/ci-fixtures" / "ci.env"),
         "-f", str(repo_root / "compose.yaml"), "-f", str(repo_root / "compose.d" / "b5.yaml"),
         "config", "--format", "json"],
        capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_docker
def test_intake_resolves_with_dummy_values_only(repo_root):
    model = _config(repo_root)
    svc = model["services"]["intake"]
    assert svc["command"] == ["python", "-m", "core.intake"]
    assert svc["user"] == "1000:1000"  # ${AX_UID}:${AX_GID}。intake は司令塔のユーザーの uid で動く（F-9.2）


@needs_docker
def test_intake_seat_mount_and_groups_use_configured_identities(repo_root, monkeypatch):
    monkeypatch.setenv("AX_SEAT_UID", "2101")
    monkeypatch.setenv("AX_SEAT_GID", "2102")
    monkeypatch.setenv("AX_HQ_GID", "2103")
    svc = _config(repo_root)["services"]["intake"]
    assert svc["environment"]["AX_SEAT_UID"] == "2101"
    assert svc["environment"]["AX_SEAT_GID"] == "2102"
    assert set(svc["group_add"]) == {"2102", "2103"}
    seat = next(v for v in svc["volumes"] if v["target"] == "/run/seat")
    assert seat["source"].endswith("/state/seat") and not seat.get("read_only")


@needs_docker
def test_intake_joins_ax_core_only_and_has_no_ports(repo_root):
    """intake は ax-core だけ（F-18.4）。ports は無い（D-14）。作業側の網からは届かない。"""
    model = _config(repo_root)
    svc = model["services"]["intake"]
    nets = svc.get("networks") or {}
    assert list(nets) == ["ax-core"] and "ax-work" not in nets and "ax-job" not in nets
    assert not svc.get("ports")


@needs_docker
def test_intake_binds_commander_dir_and_state(repo_root):
    model = _config(repo_root)
    vols = {v.get("target"): v for v in model["services"]["intake"]["volumes"]}
    assert vols["/run/commander"]["source"].endswith("state/commander")  # ディレクトリごと bind（F-9.2）
    assert vols["/run/commander"].get("read_only") is not True
    assert "/state" in vols and "/tmp-hdd" in vols
    env = model["services"]["intake"]["environment"]
    assert env["AX_UID"] == "1000" and env["AX_HQ_UID"] == "1001" and env["AX_HOST"] == "ci-host"


@needs_docker
def test_compose_check_accepts_the_b5_services(repo_root):
    model = _config(repo_root)
    bad = compose_check.check(model, allow_ports={"gate"})
    assert bad == []

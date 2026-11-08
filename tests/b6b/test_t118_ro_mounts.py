"""T-118 追加の読み取り専用マウント（F-11.5、F-18.2）。登録簿の id だけでマウントを付け、パスは呼び出し元が指定できない。

実環境でしか通せない部分（コンテナの中から書けないこと、`/tmp-hdd/moddeploy` に非 root で書けること）は status/b6b.md の表に残す。
"""
import json
import os
from pathlib import Path

import jsonschema
import pytest
import yaml

from tests.b6b.helpers import ROOT, Sandbox, flag_values


def registry(sb, items):
    (sb.ax_home / "config" / "ro-mounts.yaml").write_text(yaml.safe_dump(items))


def start_with(sb, ids, **kw):
    return sb.start("j1", runner="verify-dotnet", model=None, verify="true", ro_mounts=ids, **kw)


def test_t118_ro_mount_is_added_read_only(sb):
    code, out = start_with(sb, ["sod-gamedata"])
    assert code == 0, out
    a = sb.run_args("j1")
    vs = flag_values(a, "-v")
    assert f"{sb.gamedata}:/mnt/sod-gamedata:ro" in vs
    assert vs[:2] == [f"{sb.work_dir('j1')}:/work:rw", f"{sb.chain_dir('j1')}:/tmp-hdd:rw"]
    info = sb.fd.container("ax-job-j1")
    assert {m["Destination"]: m["RW"] for m in info["Mounts"]}["/mnt/sod-gamedata"] is False


def test_t118_target_ro_and_ro_mounts_together(sb):
    target = sb.tmp / "repo"
    target.mkdir()
    code, out = start_with(sb, ["sod-gamedata"], target_ro=str(target))
    assert code == 0, out
    vs = flag_values(sb.run_args("j1"), "-v")
    assert f"{target}:/target:ro" in vs and f"{sb.gamedata}:/mnt/sod-gamedata:ro" in vs


def test_t118_unknown_id_rejected_with_2(sb):
    code, out = start_with(sb, ["no-such-id"])
    assert code == 2 and out["error"]["reason"] == "unknown_ro_mount"
    assert sb.run_calls() == []


def test_t118_request_cannot_carry_paths(sb):
    for k in ("host_path", "container_path", "ro_paths", "mounts", "volumes"):
        code, out = start_with(sb, ["sod-gamedata"], **{k: str(sb.gamedata)})
        assert code == 2 and out["error"]["reason"] == "unknown_field", k
    code, out = start_with(sb, [str(sb.gamedata)])  # id の形でないものにパスを書いても拒否
    assert code == 2
    code, out = start_with(sb, ["a", "b", "c", "d"])
    assert code == 2
    code, out = start_with(sb, ["sod-gamedata", "sod-gamedata"])
    assert code == 2
    assert sb.run_calls() == []


def forbidden_host_paths(sb):
    return {
        "secrets_dir": str(sb.secrets),
        "inside secrets_dir": str(sb.secrets / "sub"),
        "ssh": str(sb.home / ".ssh"),
        "inside ssh": str(sb.home / ".ssh" / "keys"),
        "config": str(sb.home / ".config"),
        "inside config": str(sb.home / ".config" / "ax"),
        "state": str(sb.state),
        "inside state": str(sb.state / "deploy"),
        "ax_home": str(sb.ax_home),
        "inside ax_home": str(sb.ax_home / "config"),
        "root": "/",
        "home (contains ~/.ssh)": str(sb.home),
        "parent of secrets_dir": str(sb.tmp),
        "relative": "relative/dir",
        "dotdot": str(sb.gamedata) + "/../" + sb.gamedata.name,
    }


def test_t118_forbidden_host_paths_rejected_with_2(sb):
    for label, hp in forbidden_host_paths(sb).items():
        for d in (sb.home / ".ssh" / "keys", sb.home / ".config" / "ax", sb.secrets / "sub", sb.state / "deploy", sb.ax_home / "config"):
            d.mkdir(parents=True, exist_ok=True)
        registry(sb, [{"id": "x", "host_path": hp, "container_path": "/mnt/x", "sensitivity": "private"}])
        code, out = start_with(sb, ["x"])
        assert code == 2, (label, out)
        assert out["error"]["reason"] in ("forbidden_mount", "bad_mount"), (label, out)
    assert sb.run_calls() == []


def test_t118_symlink_into_forbidden_place_rejected(sb):
    link = sb.tmp / "innocent"
    link.symlink_to(sb.secrets)
    registry(sb, [{"id": "x", "host_path": str(link), "container_path": "/mnt/x", "sensitivity": "private"}])
    code, out = start_with(sb, ["x"])
    assert code == 2 and out["error"]["reason"] == "forbidden_mount"


@pytest.mark.parametrize("cpath", ["/work", "/work/sub", "/tmp-hdd", "/tmp-hdd/moddeploy", "/target", "/target/x", "/tmp", "/tmp/x", "/", "relative", "/a/../work"])
def test_t118_forbidden_container_paths_rejected_with_2(sb, cpath):
    registry(sb, [{"id": "x", "host_path": str(sb.gamedata), "container_path": cpath, "sensitivity": "private"}])
    code, out = start_with(sb, ["x"])
    assert code == 2, out
    assert out["error"]["reason"] in ("forbidden_mount", "bad_mount")
    assert sb.run_calls() == []


def test_t118_container_paths_must_not_overlap_each_other(sb):
    other = sb.tmp / "other"
    other.mkdir()
    registry(sb, [{"id": "a", "host_path": str(sb.gamedata), "container_path": "/mnt/a", "sensitivity": "private"},
                  {"id": "b", "host_path": str(other), "container_path": "/mnt/a/b", "sensitivity": "public"}])
    code, out = start_with(sb, ["a", "b"])
    assert code == 2 and out["error"]["reason"] == "forbidden_mount"


def test_t118_missing_host_path_is_exit_3_mount_missing(sb):
    registry(sb, [{"id": "x", "host_path": str(sb.tmp / "does-not-exist"), "container_path": "/mnt/x", "sensitivity": "private"}])
    code, out = start_with(sb, ["x"])
    assert code == 3 and out["error"]["reason"] == "mount_missing"
    assert sb.run_calls() == []
    f = sb.tmp / "afile"
    f.write_text("x")
    registry(sb, [{"id": "x", "host_path": str(f), "container_path": "/mnt/x", "sensitivity": "private"}])  # ファイルはディレクトリでない
    assert start_with(sb, ["x"])[0] == 3
    # 他の repo のジョブ（ro_mounts なし）は影響を受けない
    assert sb.start("j2", runner="verify-python", model=None, verify="true")[0] == 0


def test_t118_forbidden_target_ro(sb):
    for hp in (str(sb.secrets), str(sb.home / ".ssh"), "/", "relative"):
        (sb.home / ".ssh").mkdir(exist_ok=True)
        code, out = start_with(sb, [], target_ro=hp)
        assert code == 2, hp
    code, out = start_with(sb, [], target_ro=str(sb.tmp / "nope"))
    assert code == 3 and out["error"]["reason"] == "mount_missing"
    assert sb.run_calls() == []


def test_t118_repo_registry_and_dotnet_runner_values(repo_root):
    reg = yaml.safe_load((repo_root / "config" / "ro-mounts.yaml").read_text())
    jsonschema.validate(reg, json.loads((repo_root / "schemas/ext/b6b/ro-mounts.schema.json").read_text()))
    assert reg == [{"id": "sod-gamedata", "host_path": "~/dev/sod-gamedata", "container_path": "/mnt/sod-gamedata", "sensitivity": "private"}]
    r = yaml.safe_load((repo_root / "config/runners/verify-dotnet.yaml").read_text())
    env = r["env"]
    assert env["GameDir"] == "/mnt/sod-gamedata/Shape of Dreams"
    assert env["ModDeployDir"] == "/tmp-hdd/moddeploy"
    assert env["NUGET_PACKAGES"] == "/tmp-hdd/nuget"
    assert env["DOTNET_ROLL_FORWARD"] == "LatestMajor" and env["DOTNET_CLI_TELEMETRY_OPTOUT"] == "1" and env["DOTNET_NOLOGO"] == "1"


def test_t118_dotnet_env_with_space_reaches_docker_as_one_word(sb):
    assert start_with(sb, ["sod-gamedata"])[0] == 0
    env = flag_values(sb.run_args("j1"), "-e")
    assert "GameDir=/mnt/sod-gamedata/Shape of Dreams" in env and "ModDeployDir=/tmp-hdd/moddeploy" in env


def test_t118_home_expansion_of_registry_host_path(tmp_path, monkeypatch):
    sbx = Sandbox(tmp_path)
    (sbx.home / "dev" / "sod-gamedata").mkdir(parents=True)
    registry(sbx, yaml.safe_load((ROOT / "config" / "ro-mounts.yaml").read_text()))
    monkeypatch.setenv("HOME", str(sbx.home))  # ~ の展開は HOME を見る
    code, out = start_with(sbx, ["sod-gamedata"])
    assert code == 0, out
    assert f"{sbx.home}/dev/sod-gamedata:/mnt/sod-gamedata:ro" in flag_values(sbx.run_args("j1"), "-v")

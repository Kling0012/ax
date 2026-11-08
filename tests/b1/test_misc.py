"""B1/B2 の補助の試験: compose の束、ext スキーマ、結び付け先の選び方、docs の存在。デーモンに接続しない（docker compose config だけ）。"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# --- compose.d/b1.yaml ----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def model(tmp_path_factory=None):
    if shutil.which("docker") is None:
        pytest.skip("docker の CLI が無い")
    tmp = Path(__import__("tempfile").gettempdir()) / f"b1-compose-{os.getpid()}"
    (tmp / "compose.d").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "compose.yaml", tmp / "compose.yaml")
    shutil.copytree(ROOT / "docker", tmp / "docker", dirs_exist_ok=True)
    shutil.copy(ROOT / "compose.d" / "b1.yaml", tmp / "compose.d" / "b1.yaml")
    env = dict(os.environ, DOCKER_HOST="unix:///nonexistent")
    p = subprocess.run(["docker", "compose", "-p", "ax-ci", "--project-directory", str(tmp), "--env-file", str(ROOT / "tools/ci-fixtures/ci.env"),
                        "-f", str(tmp / "compose.yaml"), "-f", str(tmp / "compose.d/b1.yaml"), "config", "--format", "json"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert "variable is not set" not in p.stderr
    return json.loads(p.stdout)


def test_services_and_networks_follow_f184(model):
    s = model["services"]
    assert set(s) == {"archive-init", "timeline", "memory-connector", "archiver"}
    assert set(s["timeline"]["networks"]) == {"ax-core", "ax-work"} and set(s["memory-connector"]["networks"]) == {"ax-core", "ax-work"}
    assert set(s["archiver"]["networks"]) == {"ax-core"} and s["archiver"]["networks"]["ax-core"]["ipv4_address"] == "192.0.2.12"
    assert not s["archive-init"].get("networks") and s["archive-init"]["network_mode"] == "none"
    for n, svc in s.items():
        assert not svc.get("ports"), n


def test_archiver_gets_only_archive_subpaths_and_no_secrets_dir(model):
    a = model["services"]["archiver"]
    vols = {v["target"]: v for v in a["volumes"]}
    for name in ("timeline-data", "memory-connector-data", "archiver-data"):
        assert vols[f"/vol/{name}"]["volume"] == {"subpath": "archive"} and vols[f"/vol/{name}"].get("read_only") is not True
    text = json.dumps(a)
    assert "/secrets" not in json.dumps([v.get("source") for v in a["volumes"]])      # secrets_dir は渡さない（S-2.2、S-14.3）
    assert set(a["secrets"] and [x["source"] for x in a["secrets"]]) == {"archive-ssh-key", "archive-known-hosts"}
    assert a["cap_drop"] == ["ALL"] and set(a["cap_add"]) == {"DAC_OVERRIDE", "DAC_READ_SEARCH"}
    assert vols["/profile"].get("read_only") is True and "docker.sock" not in text
    assert a["depends_on"]["archive-init"]["condition"] == "service_completed_successfully"


def test_redact_index_is_read_only_for_services_that_use_redact(model):
    for n in ("timeline", "memory-connector"):
        vols = {v["target"]: v for v in model["services"][n]["volumes"]}
        assert vols["/redact"]["read_only"] is True and vols["/redact"]["source"].endswith("/redact")
        assert [x["source"] for x in model["services"][n]["secrets"]] == ["pii-list"]
        assert model["services"][n]["depends_on"]["archive-init"]["condition"] == "service_completed_successfully"


def test_archive_init_touches_only_volumes_and_the_state_archive_dir(model):
    i = model["services"]["archive-init"]
    targets = {v["target"] for v in i["volumes"]}
    assert targets == {"/v/timeline-data", "/v/memory-connector-data", "/v/archiver-data", "/hoststate"}
    cmd = " ".join(i["command"])
    assert "mkdir -p" in cmd and "chown" in cmd and "rm " not in cmd
    assert set(i["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER"} and i["restart"] == "no"


def test_compose_check_tool_accepts_it(model, tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(model))
    r = subprocess.run(["python3", str(ROOT / "tools" / "compose_check.py"), "--no-network-check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# --- ext スキーマ ------------------------------------------------------------------------------------
def test_ext_schemas_are_valid_draft_2020_12_and_refs_resolve():
    jsonschema = pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")
    from tests.b1.helpers import EXT_DIR, ext_errors
    names = sorted(p.name for p in EXT_DIR.glob("*.json"))
    assert len(names) >= 9
    for n in names:
        doc = json.loads((EXT_DIR / n).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(doc)
        assert doc["$id"] == "https://ax.invalid/schemas/ext/b1/" + n
    # $ref の解決（空のインスタンスで検査器を動かす）
    ext_errors("events-list-response", {"events": [], "next_after": 0})
    assert ext_errors("events-list-response", {"events": [{"id": "x"}], "next_after": 0})
    assert ext_errors("timeline-archive-index", {"version": 1, "files": []}) == []
    assert ext_errors("export-request", {"days": 3}) == [] and ext_errors("export-request", {"bogus": 1})


# --- 結び付け先 ---------------------------------------------------------------------------------------
def test_netbind_picks_each_network_address_only():
    from core.timeline import netbind
    addrs = ["127.0.0.1", "192.0.2.5", "198.51.100.7", "203.0.113.7"]
    core, work = netbind.resolve_binds({}, addrs)
    assert core == ["192.0.2.5", "127.0.0.1"] and work == ["198.51.100.7"]          # 8080 は ax-work 側に出さない
    core, work = netbind.resolve_binds({}, ["127.0.0.1", "192.0.2.5"])
    assert work == []                                                                  # ax-work 側が無ければ 8081 は開かない
    core, work = netbind.resolve_binds({"AX_TIMELINE_CORE_BIND": "198.51.100.9", "AX_TIMELINE_WORK_BIND": "198.51.100.8,198.51.100.7"}, addrs)
    assert core == ["198.51.100.9", "127.0.0.1"] and work == ["198.51.100.8", "198.51.100.7"]
    core, work = netbind.resolve_binds({}, [])
    assert core == ["127.0.0.1"] and work == []
    assert netbind.pick_in_subnet(addrs, "198.51.100.0/25") == ["198.51.100.7"]


def test_netbind_never_uses_all_interfaces():
    # netbind 自身は 0.0.0.0 の拒否の分岐だけに現れる。待ち受け先として 0.0.0.0 を使わないこと（D-14）。
    for p in (ROOT / "core" / "timeline" / "netbind.py", ROOT / "core" / "memory-connector" / "main.py", ROOT / "core" / "timeline" / "__main__.py"):
        assert 'host="0.0.0.0"' not in p.read_text(encoding="utf-8"), p.name


def test_core_only_binds_narrows_to_core_and_loopback():
    from core.timeline import netbind
    addrs = ["127.0.0.1", "192.0.2.5", "198.51.100.7", "203.0.113.7"]
    assert netbind.core_only_binds("AX_X_BIND", {}, addrs) == ["192.0.2.5", "127.0.0.1"]  # ax-work 側は含めない
    assert netbind.core_only_binds("AX_X_BIND", {"AX_X_BIND": "198.51.100.9"}, addrs) == ["198.51.100.9", "127.0.0.1"]
    assert netbind.core_only_binds("AX_X_BIND", {}, []) == ["127.0.0.1"]  # アドレス無しでも healthcheck 用に 127.0.0.1
    assert netbind.core_only_binds("AX_X_BIND", {"AX_X_BIND": "0.0.0.0"}, []) == ["127.0.0.1"]  # 0.0.0.0 を取らない


def test_core_only_services_do_not_bind_all_interfaces():
    # inspector / manager / observer / archiver / gate / emergency は 0.0.0.0 で待ち受けない（D-14）。
    from core.timeline import netbind
    mods = (("core.inspector.__main__", "AX_INSPECTOR_BIND"), ("core.manager.__main__", "AX_MANAGER_BIND"),
            ("core.observer.__main__", "AX_OBSERVER_BIND"), ("core.archiver.__main__", "AX_ARCHIVER_BIND"),
            ("core.gate.__main__", "AX_GATE_BIND"), ("core.intake.dest.b10a.__main__", "AX_EMERGENCY_BIND"))
    import importlib
    addrs = ["127.0.0.1", "192.0.2.5", "198.51.100.7"]
    for name, var in mods:
        m = importlib.import_module(name)
        assert m.resolve_binds({}, addrs) == ["192.0.2.5", "127.0.0.1"], name
        assert m.resolve_binds({var: "198.51.100.9"}, []) == ["198.51.100.9", "127.0.0.1"], name
        text = Path(m.__file__).read_text(encoding="utf-8")
        assert 'host="0.0.0.0"' not in text and "host='0.0.0.0'" not in text, name


# --- 実在の値を埋め込まない -------------------------------------------------------------------------------
def test_public_scope_has_no_private_identifiers():
    import re

    github_owner = "Kling" + "0012"
    fragments = (
        "kc" + "lab", "cont" + "2", "woma" + "kase", "kh" + "ome",
        "kling" + "lab", "kling" + "think", "kuf" + "home",
        "tail" + "fcd46b", "kling" + "_lab", "koneko" + "-nemu",
    )
    pattern = re.compile(
        r"(?:" + "|".join(re.escape(value) for value in fragments)
        + r"|github\.com/" + re.escape(github_owner) + r"(?:/|\b))",
        re.IGNORECASE,
    )
    public_scope = (
        "compose.yaml", "compose.d", "docker", "core", "axcommon", "runtime",
        "plugins", "schemas", "config", "host", "commander", "templates",
        "profile/example.yaml", "tools", "ci", "tests", "docs", ".github",
        "LICENSE", "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md",
        "CODE_OF_CONDUCT.md", "pyproject.toml", "uv.lock", ".gitignore",
        ".gitattributes", "PUBLIC-SCOPE.md",
    )
    for relative in public_scope:
        path = ROOT / relative
        files = (path,) if path.is_file() else (item for item in path.rglob("*") if item.is_file())
        for file in files:
            text = file.read_text(encoding="utf-8", errors="ignore")
            assert not pattern.search(text), f"{file.relative_to(ROOT)}: private identifier"

def test_registry_fragment_matches_its_schema():
    import yaml
    from axcommon import schemas
    doc = yaml.safe_load((ROOT / "plugins" / "registry.d" / "b1.yaml").read_text(encoding="utf-8"))
    assert schemas.errors("registry-fragment", doc) == [] and doc["plugins"] == []

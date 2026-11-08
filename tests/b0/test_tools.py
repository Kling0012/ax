"""tools/ の道具（build_registry、trace_check、build_index）と、リポジトリの衛生（B0）。"""
import os
import shutil
import subprocess
import sys

import pathspec
import pytest
import yaml

from tests.b0.helpers import load_tool

br = load_tool("build_registry")


@pytest.fixture
def proj(repo_root, tmp_path):
    p = tmp_path / "p"
    shutil.copytree(repo_root / "axcommon", p / "axcommon", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repo_root / "schemas", p / "schemas")
    (p / "plugins" / "registry.d").mkdir(parents=True)
    return p


def frag(proj, name, plugins):
    (proj / "plugins" / "registry.d" / f"{name}.yaml").write_text(yaml.safe_dump({"plugins": plugins}))


def test_registry_merges_fragments_sorted_by_id(proj):
    frag(proj, "b4", [{"id": "zeta", "kind": "subloop", "path": "plugins/subloop/zeta/manifest.yaml"}])
    frag(proj, "b6", [{"id": "alpha", "kind": "resource", "path": "plugins/resource/alpha/manifest.yaml"}])
    body, errors, warns = br.build(proj)
    assert errors == []
    doc = yaml.safe_load(body)
    assert [p["id"] for p in doc["plugins"]] == ["alpha", "zeta"]
    assert doc["plugins"][0]["source"] == "b6.yaml"
    assert len(warns) == 2  # manifest がまだ無い


def test_registry_rejects_duplicate_ids_across_fragments(proj):
    frag(proj, "b4", [{"id": "dup", "kind": "subloop", "path": "x"}])
    frag(proj, "b6", [{"id": "dup", "kind": "timer", "path": "y"}])
    _b, errors, _w = br.build(proj)
    assert any("重複" in e for e in errors)


def test_registry_rejects_bad_fragment_shape(proj):
    frag(proj, "b4", [{"id": "Bad_ID", "kind": "subloop", "path": "x"}])
    assert br.build(proj)[1]
    (proj / "plugins" / "registry.d" / "b5.yaml").write_text("plugins: [")
    assert any("YAML" in e for e in br.build(proj)[1])


def test_registry_checks_manifest_id_and_kind_when_manifest_exists(proj):
    m = proj / "plugins" / "subloop" / "p"
    m.mkdir(parents=True)
    (m / "manifest.yaml").write_text("id: other\nkind: subloop\n")
    frag(proj, "b4", [{"id": "p", "kind": "subloop", "path": "plugins/subloop/p/manifest.yaml"}])
    assert any("manifest の id/kind" in e for e in br.build(proj)[1])


def test_registry_write_then_check_roundtrip_and_detects_drift(proj):
    frag(proj, "b4", [{"id": "p", "kind": "subloop", "path": "x"}])
    assert br.main(["--write", "--root", str(proj)]) == 0
    assert br.main(["--check", "--root", str(proj)]) == 0
    (proj / "plugins" / "registry.yaml").write_text("schema_version: 1\nplugins: []\n")
    assert br.main(["--check", "--root", str(proj)]) == 1


def test_repo_registry_is_in_sync_with_fragments(repo_root):
    assert br.main(["--check", "--root", str(repo_root)]) == 0


def require_design_docs(repo_root, names):
    if any(not (repo_root / "docs" / f"{name}.md").is_file() for name in names):
        pytest.skip("design documents unavailable")


DESIGN_DOCS = ("REQUIREMENTS", "SPEC", "DESIGN", "FACTS", "ENV-MODULE", "CONSISTENCY", "INDEX")


def test_trace_check_passes_on_docs_copy(repo_root):
    require_design_docs(repo_root, DESIGN_DOCS)
    r = subprocess.run([sys.executable, str(repo_root / "tools" / "trace_check.py"), str(repo_root / "docs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_build_index_check_passes_on_docs_copy(repo_root):
    require_design_docs(repo_root, DESIGN_DOCS)
    r = subprocess.run([sys.executable, str(repo_root / "tools" / "build_index.py"), "--check", str(repo_root / "docs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_docs_copies_are_identical_to_final_edition_when_source_is_present(repo_root):
    src = repo_root.parent.parent / "final"
    if not src.is_dir():
        pytest.skip("設計文書の元（final/）が無い")
    require_design_docs(repo_root, ("REQUIREMENTS", "SPEC", "DESIGN", "FACTS", "ENV-MODULE"))
    for n in ("REQUIREMENTS", "SPEC", "DESIGN", "FACTS", "ENV-MODULE"):
        assert (src / f"{n}.md").read_bytes() == (repo_root / "docs" / f"{n}.md").read_bytes(), n


def test_docs_hold_required_files(repo_root):
    for n in ("test-procedure", "CHANGELOG"):
        assert (repo_root / "docs" / f"{n}.md").is_file()
    assert not list((repo_root / "docs").glob("*.bak-*"))


@pytest.mark.parametrize("name", ("REQUIREMENTS", "SPEC", "DESIGN", "FACTS", "ENV-MODULE"))
def test_docs_hold_required_design_file(repo_root, name):
    require_design_docs(repo_root, (name,))
    assert (repo_root / "docs" / f"{name}.md").is_file()


def test_changelog_is_append_only_shape(repo_root):
    text = (repo_root / "docs" / "CHANGELOG.md").read_text()
    assert text.startswith("# 実装者の判断の記録")
    assert text.count("\n- 2026-") >= 10


def test_standing_rules_cover_s162_items(repo_root):
    t = (repo_root / "config" / "standing-rules.md").read_text()
    for needle in ("前景", "全テストを回さない", "変更した部分だけ", "ハッシュ", "push", "戻しにくい", "TMPDIR", "日本語", "STATUS:", "テストを書かない"):
        assert needle in t, needle
    assert t.index("テストを書かない") > t.index("### impl")  # impl だけに足す


def test_gitignore_keeps_only_example_profile_and_ignores_secrets(repo_root):
    spec = pathspec.GitIgnoreSpec.from_lines((repo_root / ".gitignore").read_text().splitlines())

    def ignored(p):
        return spec.match_file(p)

    assert ignored("profile/hub-host.yaml") and ignored("profile.env") and ignored("secrets/token")
    assert not ignored("profile/example.yaml") and not ignored("tools/ci-fixtures/ci.env") and not ignored("tools/ci-fixtures/secrets/jev-key")


def test_ci_locks_are_verified_and_build_guard_remains(repo_root):
    for f in ("base-image.lock", "docker-cli.lock"):
        t = (repo_root / "ci" / f).read_text()
        assert "digest=sha256:" in t and "status=verified" in t and "checked_at=" in t
    build_script = (repo_root / "ci" / "build-ax-ci.sh").read_text()
    assert '""|UNVERIFIED)' in build_script  # 固定していないイメージを黙って組み立てない


def test_ax_ci_dockerfile_pins_via_args_and_copies_cli_from_official_image(repo_root):
    t = (repo_root / "ci" / "Dockerfile.ax-ci").read_text()
    assert "docker:29.1.3-cli" in t and "python:3.12-slim" in t
    assert "COPY --from=dockercli /usr/local/bin/docker" in t and "docker-compose" in t
    assert ":latest" not in t  # 版の決まらない取得を使わない（F-14.1）
    assert "apt-get" not in t and "apt install" not in t  # apt のパッケージを使わない（F-14.1）


def test_core_dockerfile_is_one_image_without_service_specific_command(repo_root):
    t = (repo_root / "docker" / "Dockerfile.core").read_text()
    assert t.count("\nCMD ") == 1 and "axcommon.selfcheck" in t
    assert "EXPOSE" not in t  # ポートの公開は compose の ports（${AX_BIND_ADDR}:…）だけ


def test_core_dockerignore_excludes_tests_secrets_and_profile(repo_root):
    t = (repo_root / "docker" / "Dockerfile.core.dockerignore").read_text().split()
    for x in ("tests", "docs", "profile", "secrets", ".git"):
        assert x in t


def test_github_workflow_is_manual_only_until_d17_confirmation(repo_root):
    doc = yaml.safe_load((repo_root / ".github" / "workflows" / "ci.yml").read_text())
    triggers = doc.get(True) or doc.get("on")  # YAML 1.1 では on が True になる
    assert list(triggers) == ["workflow_dispatch"]


def test_repo_has_no_secret_shaped_values(repo_root):
    """S-15.3 の鍵・トークンの形の一部を、リポジトリの全文（docs/ を除く）で検査する。試験のソースには実値を書かない（連結で作る）。"""
    import re

    pats = [r"sk-[A-Za-z0-9_-]{20,}", r"gh[pousr]_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{40,}", r"AKIA[0-9A-Z]{16}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"axg-[0-9a-f]{64}", "eyJ" + r"[A-Za-z0-9_-]{10,}\." + r"[A-Za-z0-9_-]{10,}\."]
    ignored = pathspec.GitIgnoreSpec.from_lines((repo_root / ".gitignore").read_text().splitlines())
    skip_dirs = {".git", "docs", "__pycache__"}
    for root, dirs, files in os.walk(repo_root):
        rel_root = os.path.relpath(root, repo_root)
        dirs[:] = [d for d in dirs if d not in skip_dirs and not ignored.match_file(f"{rel_root}/{d}/" if rel_root != "." else f"{d}/")]
        for f in files:
            rel_path = os.path.relpath(os.path.join(root, f), repo_root)
            if f.endswith((".pyc", ".lock")) or f == "test_tools.py" or ignored.match_file(rel_path):
                continue
            text = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
            for p in pats:
                assert not re.search(p, text), (f, p)

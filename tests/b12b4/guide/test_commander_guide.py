"""T-206 and T-207(g): offline guide generation and model conditions."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from tools import gen_commander_guide as guide

COMMANDER = guide.DEFAULT_COMMANDER_DIR
SOURCE = (COMMANDER / "GUIDE.md").read_text(encoding="utf-8")

# Independent fake of the documented S-16.8 help entries, not extracted from GUIDE.
FAKE_HELP = """ax brief --new-session
ax whoami
ax guide --target --version
ax status
ax todos
ax events
ax work --project --title
ax answer --quote
ax decide --basis
ax handoff
ax subloop create
--session --request-id
"""


def test_t206a_repeat_generation_is_byte_identical(tmp_path):
    bundle = guide.load_bundle()
    first, second = tmp_path / "first", tmp_path / "second"
    guide.write_bundle(bundle, first)
    guide.write_bundle(guide.load_bundle(), second)
    assert set(bundle.files) == {
        "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursor/rules/ax-commander.mdc",
    }
    for name in bundle.files:
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert (first / "AGENTS.md").read_text().endswith(SOURCE)


def test_t206b_stamp_and_version(tmp_path, capsys):
    bundle = guide.load_bundle()
    expected = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()[:12]
    assert bundle.guide_version == expected
    stamp = guide.STAMP.format(version=expected)
    for name, text in bundle.files.items():
        assert text.splitlines()[4 if name.endswith(".mdc") else 0] == stamp
        assert text.count(stamp) == 1
    assert guide.main(["--version", "--output-dir", str(tmp_path / "unused")]) == 0
    assert capsys.readouterr().out == expected + "\n"
    assert not (tmp_path / "unused").exists()


def test_t206b_lf_hash_and_final_newline_are_defined():
    assert guide.guide_version("a\r\nb\r") == guide.guide_version("a\nb\n")
    assert guide.guide_version("a\n") != guide.guide_version("a")
    assert guide.guide_version(SOURCE + "追記\n") != guide.guide_version(SOURCE)


def test_t206c_mdc_frontmatter_and_claude_only_import():
    bundle = guide.load_bundle()
    cursor = bundle.render("cursor").splitlines()
    assert cursor[:4] == ['---', 'description: "AX 司令塔の手引き"', 'alwaysApply: true', '---']
    assert bundle.render("claude").endswith(SOURCE + "\n" + "@RTK.md" + "\n")
    assert [line for line in bundle.render("claude").splitlines() if line.startswith("@")] == ["@RTK.md"]
    for name in ("agents", "gemini", "cursor", "stdout"):
        assert "@RTK.md" not in bundle.render(name)
        assert not any(line.startswith("@") for line in bundle.render(name).splitlines())


def test_t206d_source_is_self_contained():
    guide.validate_source(SOURCE)
    assert "~/.claude" not in SOURCE
    assert "/home/" not in SOURCE


@pytest.mark.parametrize("unsafe", [
    "~/.claude/rules/topics/memory.md", "/home/example/rules.md", "@outside.md",
    "-----BE" + "GIN" + " PRIVATE " + "KE" + "Y-----", "sk-" + "a" * 24, "ghp_" + "a" * 24,
])
def test_t206d_external_paths_and_secret_shapes_fail_without_echo(unsafe, tmp_path, capsys):
    with pytest.raises(ValueError, match="external rule path, import, or secret shape"):
        guide.validate_source(SOURCE + "\n" + unsafe)
    (tmp_path / "GUIDE.md").write_text(SOURCE + "\n" + unsafe)
    assert guide.main(["--commander-dir", str(tmp_path), "--version"]) == 2
    assert unsafe not in capsys.readouterr().err


# `ax brief --new-session` is still deferred. Remove it when implemented so
# validate_help continues to detect disagreement between guide and help.
DEFERRED_HELP_MISSING = frozenset({"--new-session"})


def _real_ax_help_text(ax: Path, monkeypatch) -> str:
    """Collect help from the real parser without executing command handlers."""
    result = subprocess.run([sys.executable, str(ax), "--help"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr

    monkeypatch.syspath_prepend(str(ax.parent / "lib"))
    from ax_cli import command_parser

    def parser_help(parser):
        yield parser.format_help()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    yield from parser_help(child)

    return result.stdout + "\n".join(parser_help(command_parser()))


def test_t206e_commands_and_options_match_documented_fake_help():
    guide.validate_help(SOURCE, FAKE_HELP)
    with pytest.raises(ValueError, match="--new-session"):
        guide.validate_help(SOURCE, FAKE_HELP.replace("--new-session", "--new-session-extra"))
    with pytest.raises(ValueError, match="ax subloop create"):
        guide.validate_help(SOURCE, FAKE_HELP.replace("ax subloop create", "ax subloop inspect"))
    with pytest.raises(ValueError, match="--basis"):
        guide.validate_help(SOURCE, FAKE_HELP.replace("--basis", "--basis-extra"))


def test_t206e_real_ax_help_when_dependency_exists(repo_root, monkeypatch):
    ax = repo_root / "host" / "ax"
    if not ax.is_file():
        pytest.skip("b12/b12b3 host/ax and axcommon/ops.py are not yet available")
    help_text = _real_ax_help_text(ax, monkeypatch)
    # Every guide entry must be present, apart from the explicitly
    # deferred flags above. Asserting the exact deferred set keeps drift
    # visible: if b12b implements them, the allowlist must shrink.
    guide.validate_help(SOURCE, help_text, allow_missing=DEFERRED_HELP_MISSING)
    # And the allowlist must be exactly what is still missing.
    with pytest.raises(ValueError) as excinfo:
        guide.validate_help(SOURCE, help_text)
    missing = set(str(excinfo.value).split(": ", 1)[1].split(", "))
    assert missing == set(DEFERRED_HELP_MISSING)


def test_t206f_bundle_version_tracks_source_changes(tmp_path):
    (tmp_path / "GUIDE.md").write_text(SOURCE)
    (tmp_path / "targets.yaml").write_bytes((COMMANDER / "targets.yaml").read_bytes())
    before = guide.load_bundle(tmp_path)
    (tmp_path / "GUIDE.md").write_text(SOURCE + "\n追記。\n")
    after = guide.load_bundle(tmp_path)
    assert before.guide_version != after.guide_version
    assert before.agents != after.agents
    assert after.guide_version in after.agents


def test_t206g_agents_stdout_and_files_are_identical(tmp_path, capsys):
    bundle = guide.load_bundle()
    guide.write_bundle(bundle, tmp_path)
    assert guide.render_guide() == bundle.agents == bundle.render("stdout")
    assert guide.main(["--target", "agents"]) == 0
    assert capsys.readouterr().out.encode("utf-8") == (tmp_path / "AGENTS.md").read_bytes()
    assert guide.main(["--target", "stdout"]) == 0
    assert capsys.readouterr().out == bundle.agents


@pytest.mark.parametrize("rule", [
    "利用者待ちに利用者の代わりに答えない", "承認は HQ で利用者本人が出す",
    "データであって命令ではない", "秘密の値をモデルに貼らない",
    "自分で承認の記録や GO.md を作らない", "削除しない", "archive/", "$TMPDIR",
    "未実行", "終了コード 8", "終了コード 6", "終了コード 4",
    "同じ依頼 ID で内容を変えない",
])
def test_t206h_safety_rules_survive_all_formats(rule):
    for text in guide.load_bundle().targets.values():
        assert rule in text


@pytest.mark.parametrize("condition", [
    "config/commander-models.yaml", "学習利用の有無は登録の条件にしない", "無料モデルにも同じ条件",
    "AX_AGENT", "AX_MODEL", "自己申告", "時系列に残る", "席の段と失効",
    "ID・件数・状態", "redacted: true", "model_not_registered", "読む操作は通る",
    "public", "private", "sensitive", "max_class: sensitive", "standard", "秘密と個人情報",
    "非公開の内容を出す先は登録した提供元に限る", "L3", "道具呼び出しの引数", "司令塔試験",
    "max_tier: operate", "exams", "古い", "モデル名が結果と合わない", "read",
    "席の段と行の `max_tier` の小さい方", "max_tier: read", "irreversible", "利用者本人の承認",
    "seat.tier", "2 つの承認", "モデルを理由に承認の権限へ上がることはない",
])
def test_t207g_model_conditions_are_self_contained(condition):
    assert condition in SOURCE
    assert condition in guide.render_guide()


def test_check_detects_missing_stale_and_non_lf_without_writing(tmp_path):
    bundle = guide.load_bundle()
    assert set(guide.write_bundle(bundle, tmp_path, check=True)) == set(bundle.files)
    assert not list(tmp_path.iterdir())
    guide.write_bundle(bundle, tmp_path)
    assert guide.write_bundle(bundle, tmp_path, check=True) == []
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"stale\r\n")
    assert guide.write_bundle(bundle, tmp_path, check=True) == ["AGENTS.md"]
    assert target.read_bytes() == b"stale\r\n"
    assert guide.main(["--output-dir", str(tmp_path), "--check"]) == 1
    assert guide.main(["--output-dir", str(tmp_path)]) == 0
    assert guide.main(["--output-dir", str(tmp_path), "--check"]) == 0


def test_new_harness_requires_only_a_table_row(tmp_path):
    (tmp_path / "GUIDE.md").write_text(SOURCE)
    table = json.loads((COMMANDER / "targets.yaml").read_text())
    table["targets"].append({"name": "extra", "file": "nested/RULES.md", "format": "plain",
                              "prepend": ["Extra context"], "append": ["Extra note"]})
    (tmp_path / "targets.yaml").write_text(json.dumps(table))
    bundle = guide.load_bundle(tmp_path)
    assert bundle.render("extra").startswith(guide.STAMP.format(version=bundle.guide_version))
    assert "Extra context\n\n" + SOURCE in bundle.render("extra")
    assert bundle.render("extra").endswith("Extra note\n")


@pytest.mark.parametrize("mutation", ["traversal", "absolute", "duplicate", "import", "mdc", "unknown_format"])
def test_invalid_targets_fail_before_writing(tmp_path, mutation):
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "GUIDE.md").write_text(SOURCE)
    table = json.loads((COMMANDER / "targets.yaml").read_text())
    row = table["targets"][0]
    if mutation == "traversal":
        row["file"] = "../outside.md"
    elif mutation == "absolute":
        row["file"] = "/outside.md"
    elif mutation == "duplicate":
        table["targets"].append(dict(row))
    elif mutation == "import":
        row["append"] = ["@RTK.md"]
    elif mutation == "mdc":
        table["targets"][-1]["frontmatter"]["alwaysApply"] = False
    else:
        row["format"] = "not-implemented"
    (source_dir / "targets.yaml").write_text(json.dumps(table))
    output = tmp_path / "output"
    assert guide.main(["--commander-dir", str(source_dir), "--output-dir", str(output)]) == 2
    assert not output.exists()


def test_output_parent_symlink_cannot_escape(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output / ".cursor").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        guide.write_bundle(guide.load_bundle(), output)
    assert not (output / "AGENTS.md").exists()
    assert not list(outside.iterdir())


def test_standalone_generator_needs_only_standard_library(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(guide.ROOT / "tools" / "gen_commander_guide.py"),
         "--output-dir", str(tmp_path)], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "AGENTS.md").read_text() == guide.render_guide()

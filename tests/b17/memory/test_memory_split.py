"""T-151: partition only temporary copies; no services or live-memory writes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from tools import memory_split as split
from tests.fakes.b17.memory.fixtures import DAY, TABLE, memory_copy, reference_content


MOVE_ROW = "- [old workflow](worker-pool.md) — old text\n"
KEEP_ROW = "- [keep exactly](always-japanese.md) — 文 字 ** ` x `\n"
BASIC = ("## Workflow & workers\n\n" + MOVE_ROW + KEEP_ROW + "\n\n").encode()


def apply_copy(tree):
    return split.split_memory(tree.memory, TABLE, tree.output, backup=tree.backup, apply=True, day=DAY)


def test_t151_a_reference_119_and_individual_notes_unchanged(tmp_path):
    original = reference_content()
    tree = memory_copy(tmp_path, original)
    note = tree.memory.parent / "worker-pool.md"
    note.write_bytes(b"individual note, never moved\n")
    plan = split.make_plan(original.decode(), split.load_table(TABLE))
    before_stat = tree.memory.stat()
    result = apply_copy(tree)
    assert result["status"] == "applied"
    assert result["counts"] == {"n0": 119, "moved": 43, "kept": 76, "unlisted": 0, "nolink": 0}
    assert len(result["checks"]) == 4 and all(c["ok"] for c in result["checks"])
    assert result["backup_verified"]
    assert tree.backup.read_bytes() == original
    assert tree.output.read_bytes() == (
        f"AX 切り替え時に移した旧ワークフローの索引（{DAY}）\n\n" + "".join(plan.moved)
    ).encode()
    after = tree.memory.read_bytes().decode()
    assert after == "".join(plan.remaining) + "\n## Archived\n" + (
        f"- [旧ワークフローの索引](archive/{tree.output.name}) — AX への切り替えで外した"
        "旧ワークフロー（task-launcher、worker-pool、issue-automation、ToDo 台帳など）の索引\n"
    )
    assert note.read_bytes() == b"individual note, never moved\n"
    assert tree.memory.stat().st_ino != before_stat.st_ino  # os.replace, not truncation
    assert stat.S_IMODE(tree.memory.stat().st_mode) == stat.S_IMODE(before_stat.st_mode)


def test_t151_a_actual_snapshot_copy(tmp_path):
    """Optional captured snapshot; never import or discover a user's home."""
    snapshot = os.environ.get("AX_MEMORY_T151_SNAPSHOT")
    if not snapshot:
        pytest.skip("AX_MEMORY_T151_SNAPSHOT not set; synthetic baseline is always tested")
    source = Path(snapshot).resolve()
    assert source.is_relative_to(Path(os.environ["TMPDIR"]).resolve())
    original = source.read_bytes()
    tree = memory_copy(tmp_path, original)
    result = apply_copy(tree)
    assert result["status"] == "applied"
    assert (result["counts"]["n0"], result["counts"]["moved"], result["counts"]["kept"]) == (119, 43, 76)
    assert all(c["ok"] for c in result["checks"])
    assert tree.backup.read_bytes() == original
    assert source.read_bytes() == original


def test_t151_b_unlisted_is_kept_without_failure(tmp_path):
    added = "- [new fact](unlisted-project-fact.md) — keep\n"
    tree = memory_copy(tmp_path, BASIC + added.encode())
    result = apply_copy(tree)
    assert result["exit_code"] == 0
    assert result["unlisted"] == ["unlisted-project-fact.md"]
    assert added.encode() in tree.memory.read_bytes()


def test_t151_c_absent_table_rows_are_ignored(tmp_path):
    tree = memory_copy(tmp_path, MOVE_ROW.encode())
    result = apply_copy(tree)
    assert result["status"] == "applied"
    assert result["counts"]["n0"] == 1
    assert result["counts"]["moved"] == 1
    assert result["counts"]["kept"] == 0


def test_t151_d_missing_link_and_nonitems_preserved(tmp_path):
    no_link = "- [old worker-pool without link] keep by default\n"
    nonitems = "# Title\n- ordinary bullet\n  - [indented](worker-pool.md)\nexplanation\n\n"
    tree = memory_copy(tmp_path, (nonitems + MOVE_ROW + no_link + KEEP_ROW).encode())
    result = apply_copy(tree)
    assert result["status"] == "applied"
    assert result["counts"]["n0"] == 3
    assert len(result["nolink"]) == 1
    after = tree.memory.read_bytes()
    assert nonitems.encode() in after and no_link.encode() in after


def test_t151_e_existing_output_is_exit_2_without_changes(tmp_path):
    tree = memory_copy(tmp_path, BASIC)
    tree.output.parent.mkdir()
    tree.output.write_bytes(b"existing archive, must never overwrite\n")
    before = tree.memory.stat()
    result = apply_copy(tree)
    assert result["exit_code"] == 2 and result["reason"] == "output-exists"
    assert tree.memory.read_bytes() == BASIC
    assert tree.memory.stat().st_mtime_ns == before.st_mtime_ns
    assert tree.output.read_bytes() == b"existing archive, must never overwrite\n"
    assert result["staging_file"] is None


def test_t151_e_caller_selects_numbered_output_without_overwrite(tmp_path):
    tree = memory_copy(tmp_path, BASIC)
    tree.output.parent.mkdir()
    tree.output.write_bytes(b"one")
    second = split.available_output(tree.memory, DAY)
    assert second.name.endswith("-2.md")
    second.write_bytes(b"two")
    third = split.available_output(tree.memory, DAY)
    assert third.name.endswith("-3.md")
    result = split.split_memory(tree.memory, TABLE, third, backup=tree.backup, apply=True, day=DAY)
    assert result["status"] == "applied"
    assert tree.output.read_bytes() == b"one" and second.read_bytes() == b"two"
    assert f"archive/{third.name}".encode() in tree.memory.read_bytes()


def test_t151_f_repeat_is_noop_without_new_archive_or_duplicate_heading(tmp_path):
    tree = memory_copy(tmp_path, BASIC)
    assert apply_copy(tree)["status"] == "applied"
    before = tree.memory.read_bytes()
    before_stat = tree.memory.stat()
    archive = tree.output.read_bytes()
    result = apply_copy(tree)  # same output and original backup are deliberately reused
    assert result["status"] == "noop" and result["exit_code"] == 0
    assert result["counts"]["moved"] == 0
    assert tree.memory.read_bytes() == before
    assert tree.memory.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert before.count(b"## Archived\n") == 1
    assert tree.output.read_bytes() == archive
    assert result["staging_file"] is None


@pytest.mark.parametrize("which", range(4))
def test_t151_g_each_invariant_blocks_only_that_memory(tmp_path, monkeypatch, which):
    """Inject a corrupt candidate; duplicates alone are valid S-24.3 rows."""
    bad = memory_copy(tmp_path, BASIC, "bad")
    good = memory_copy(tmp_path, MOVE_ROW.encode(), "good")
    real_make = split.make_plan

    def damaged(original, verdicts):
        plan = real_make(original, verdicts)
        if original.encode() != BASIC:
            return plan
        if which == 0:
            return replace(plan, moved=plan.moved + plan.moved)
        if which == 1:
            return replace(plan, moved=("- [invented](never-present.md)\n",))
        if which == 2:
            return replace(plan, remaining=plan.remaining + plan.moved)
        return replace(plan, remaining=tuple(line.replace("keep exactly", "CHANGED") for line in plan.remaining))

    monkeypatch.setattr(split, "make_plan", damaged)
    with ThreadPoolExecutor(max_workers=2) as executor:
        bad_future = executor.submit(apply_copy, bad)
        good_future = executor.submit(apply_copy, good)
        rejected, accepted = bad_future.result(), good_future.result()
    assert rejected["reason"] == "verification-failed"
    assert not rejected["checks"][which]["ok"]
    assert rejected["todo_required"]
    assert bad.memory.read_bytes() == BASIC
    assert not bad.output.exists() and not bad.output.parent.exists()
    assert rejected["staging_file"] is None
    assert accepted["status"] == "applied"


def test_t151_h_other_project_row_is_in_table(tmp_path):
    row = "- [old automation](issue-automation-v2-deployed.md)\n"
    tree = memory_copy(tmp_path, row.encode(), "other-project")
    result = apply_copy(tree)
    assert result["counts"]["moved"] == 1 and result["status"] == "applied"
    assert row.encode() in tree.output.read_bytes()
    assert row.encode() not in tree.memory.read_bytes()
    assert b"## Archived\n" in tree.memory.read_bytes()


def test_t151_i_order_headings_blank_lines_and_kept_bytes(tmp_path):
    content = ("# Title\r\n\r\n" + MOVE_ROW.replace("\n", "\r\n") + "## Next\r\n"
               + KEEP_ROW.replace("\n", "\r\n") + "\r\n\r\n").encode()
    tree = memory_copy(tmp_path, content)
    result = apply_copy(tree)
    assert result["status"] == "applied"
    retained = content.replace(MOVE_ROW.replace("\n", "\r\n").encode(), b"")
    assert tree.memory.read_bytes().startswith(retained)
    assert tree.backup.read_bytes() == content


def test_duplicate_names_and_identical_rows_are_each_partitioned(tmp_path):
    content = (MOVE_ROW + MOVE_ROW + MOVE_ROW.replace("old text", "different text") + KEEP_ROW).encode()
    tree = memory_copy(tmp_path, content)
    result = apply_copy(tree)
    assert result["status"] == "applied" and result["counts"]["moved"] == 3
    assert tree.output.read_bytes().count(MOVE_ROW.encode()) == 2
    assert tree.memory.read_bytes().count(MOVE_ROW.encode()) == 0


def test_existing_archived_section_gets_link_before_next_section(tmp_path):
    content = ("## Archived\n\n- [earlier](archive/earlier.md)\n## Later\n" + KEEP_ROW + MOVE_ROW).encode()
    tree = memory_copy(tmp_path, content)
    assert apply_copy(tree)["status"] == "applied"
    after = tree.memory.read_bytes().decode()
    assert after.count("## Archived\n") == 1
    assert after.index(f"archive/{tree.output.name}") < after.index("## Later\n")
    assert "- [earlier](archive/earlier.md)\n" in after


@pytest.mark.parametrize("case", ["missing", "mismatch", "same-file", "hardlink", "symlink"])
def test_original_copy_must_be_readable_distinct_and_exact(tmp_path, case):
    tree = memory_copy(tmp_path, BASIC)
    backup = tree.backup
    if case == "missing":
        backup = tmp_path / "not-created.md"
    elif case == "mismatch":
        backup.write_bytes(b"different original")
    elif case == "same-file":
        backup = tree.memory
    elif case == "hardlink":
        backup = tmp_path / "hardlink.md"
        backup.hardlink_to(tree.memory)
    else:
        backup = tmp_path / "symlink.md"
        backup.symlink_to(tree.backup)
    result = split.split_memory(tree.memory, TABLE, tree.output, backup=backup, apply=True)
    assert result["exit_code"] == 1 and result["todo_required"]
    assert not tree.output.exists() and tree.memory.read_bytes() == BASIC


def test_apply_requires_backup_but_default_dry_run_never_writes(tmp_path):
    tree = memory_copy(tmp_path, BASIC)
    before = tree.memory.stat()
    result = split.split_memory(tree.memory, TABLE, Path("/dev/null"))
    assert result["status"] == "planned" and result["exit_code"] == 0
    assert all(c["ok"] for c in result["checks"])
    assert not tree.output.parent.exists()
    result = split.split_memory(tree.memory, TABLE, tree.output, apply=True)
    assert result["reason"] == "backup-required"
    assert tree.memory.read_bytes() == BASIC and tree.memory.stat().st_mtime_ns == before.st_mtime_ns


def test_remaining_item_without_final_newline_fails_exact_row_check(tmp_path):
    content = (MOVE_ROW + KEEP_ROW.rstrip("\n")).encode()
    tree = memory_copy(tmp_path, content)
    result = apply_copy(tree)
    assert result["reason"] == "verification-failed"
    assert not result["checks"][3]["ok"] and not tree.output.exists()
    assert tree.memory.read_bytes() == content


@pytest.mark.parametrize("content", ["broken row\n", "x.md 未定\n", "x.md 移す\nx.md 残す\n"])
def test_invalid_table_does_not_modify_memory(tmp_path, content):
    tree = memory_copy(tmp_path, BASIC)
    table = tmp_path / "bad-table.txt"
    table.write_text(content)
    result = split.split_memory(tree.memory, table, tree.output, backup=tree.backup, apply=True)
    assert result["exit_code"] == 1
    assert not tree.output.exists() and tree.memory.read_bytes() == BASIC


def test_cli_json_and_isolated_standard_library_execution(tmp_path, repo_root):
    tree = memory_copy(tmp_path, BASIC)
    process = subprocess.run(
        [sys.executable, "-I", str(repo_root / "tools/memory_split.py"), str(tree.memory), str(TABLE),
         str(tree.output), "--backup", str(tree.backup), "--apply", "--json"],
        capture_output=True, text=True, check=False,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["subject"] == "memory-split" and result["status"] == "applied"
    assert process.stderr == ""


def test_cli_text_reports_unlisted_and_four_ok_lines(tmp_path, capsys):
    tree = memory_copy(tmp_path, BASIC + b"- [new](new.md)\n")
    assert split.main([str(tree.memory), str(TABLE), str(tree.output)]) == 0
    output = capsys.readouterr().out
    assert "unlisted: new.md" in output
    assert output.count("OK  ") == 4
    assert not tree.output.exists()


def test_output_symlink_is_collision_and_source_symlink_is_refused(tmp_path):
    tree = memory_copy(tmp_path, BASIC)
    tree.output.parent.mkdir()
    tree.output.symlink_to(tmp_path / "dangling-target")
    result = apply_copy(tree)
    assert result["exit_code"] == 2
    assert split.available_output(tree.memory, DAY).name.endswith("-2.md")
    alias = tmp_path / "alias.md"
    alias.symlink_to(tree.memory)
    result = split.split_memory(alias, TABLE, tree.output, backup=tree.backup, apply=True)
    assert result["reason"] == "memory-not-regular"
    assert tree.memory.read_bytes() == BASIC


def test_atomic_failure_preserves_original_and_recovery_files(tmp_path, monkeypatch):
    tree = memory_copy(tmp_path, BASIC)

    def interrupted(*args):
        raise OSError("injected before atomic replace")

    monkeypatch.setattr(split.os, "replace", interrupted)
    result = apply_copy(tree)
    assert result["exit_code"] == 1 and result["output_created"]
    assert tree.memory.read_bytes() == BASIC and tree.backup.read_bytes() == BASIC
    assert tree.output.exists() and Path(result["staging_file"]).exists()


def test_source_change_detected_before_any_archive_write(tmp_path, monkeypatch):
    tree = memory_copy(tmp_path, BASIC)
    monkeypatch.setattr(split, "_source_unchanged", lambda *args: False)
    result = apply_copy(tree)
    assert result["reason"] == "source-changed"
    assert tree.memory.read_bytes() == BASIC and not tree.output.exists()
    assert Path(result["staging_file"]).exists()


def test_different_temp_filesystem_is_blocked_before_writes(tmp_path, monkeypatch):
    tree = memory_copy(tmp_path, BASIC)
    original_stat = Path.stat
    temp_root = Path(os.environ["TMPDIR"])

    def other_device(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == temp_root:
            fields = list(value)
            fields[2] = value.st_dev + 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, "stat", other_device)
    result = apply_copy(tree)
    assert result["reason"] == "atomic-replace-cross-device"
    assert not tree.output.exists() and tree.memory.read_bytes() == BASIC
    assert result["staging_file"] is None

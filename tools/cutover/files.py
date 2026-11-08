"""File operations for stages 1 and 3. Caller must hold cutover authority."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sqlite3

from .common import Context, CutoverError


def _storage_inventory() -> dict:
    import yaml

    path = Path(os.environ.get("AX_CUTOVER_INVENTORY") or Path(__file__).with_name("inventory.yaml"))
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.safe_load(stream)["files"]["storage"]
    except FileNotFoundError as exc:
        raise CutoverError(f"cutover inventory missing: {path}") from exc


def copy_preserving_original(source: Path, dest: Path) -> None:
    """Exclusive copy, no delete, no overwrite of a previous snapshot."""
    if source.is_symlink() or dest.is_symlink():
        raise CutoverError("snapshot_symlink")
    if source.is_dir():
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        for child in sorted(source.iterdir()):
            copy_preserving_original(child, dest / child.name)
        return
    contents = source.read_bytes()
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if dest.exists():
        if dest.read_bytes() != contents:
            raise CutoverError("snapshot_conflict")
        return
    with dest.open("xb") as f:
        dest.chmod(0o600)
        f.write(contents)
        f.flush()
        import os
        os.fsync(f.fileno())
    if dest.read_bytes() != contents:
        raise CutoverError("snapshot_unreadable")


def copy_originals(ctx: Context, home: Path) -> None:
    if ctx.dry_run:
        return
    ctx.guard((7,))
    files = [(home / ".claude/settings.json", "settings/settings.json"),
             (home / ".claude/CLAUDE.md", "claude-md/CLAUDE.md"),
             (home / ".claude/RTK.md", "claude-md/RTK.md"),
             (home / ".claude.json", "mcp/.claude.json"),
             (home / ".claude/rules", "rules"),
             (home / ".claude/hub/ROLE.md", "rules/ROLE.md")]
    for source, relative in files:
        copy_preserving_original(source, ctx.archive_dir / relative)
    for source in sorted((home / ".claude/projects").glob("*/memory/MEMORY.md")):
        copy_preserving_original(source, ctx.archive_dir / "memory-index" / source.parents[1].name / "MEMORY.md")


def copy_old_todo(ctx: Context, source: Path, *, writer_stopped: bool) -> None:
    if ctx.dry_run:
        return
    ctx.guard()
    if writer_stopped is not True:
        raise CutoverError("todo_writer_running")
    dest = ctx.archive_dir / "todo" / source.name
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(source) + suffix)
        if candidate.exists():
            copy_preserving_original(candidate, Path(str(dest) + suffix))
    if not dest.is_file():
        raise CutoverError("todo_missing")
    with sqlite3.connect(dest.as_uri() + "?mode=ro", uri=True) as conn:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise CutoverError("todo_unreadable")


def without_pending_guard(settings: dict) -> dict:
    """Remove only the named hook, preserving dev-flow, RTK and unrelated data."""
    result = json.loads(json.dumps(settings))
    for entries in result.get("hooks", {}).values():
        for entry in entries:
            kept = []
            for hook in entry.get("hooks", []):
                try:
                    words = shlex.split(hook.get("command", ""))
                except ValueError as exc:
                    raise CutoverError("hook_command_invalid") from exc
                # Match the executable or the Python script, not arbitrary arguments.
                script = words[0] if words else ""
                if words and Path(words[0]).name in ("python", "python3", "python3.12", "python3.13"):
                    args = [w for w in words[1:] if not w.startswith("-")]
                    script = args[0] if args else ""
                if Path(script).name != "pending-guard.py":
                    kept.append(hook)
            entry["hooks"] = kept
    return result


def place_entry(ctx: Context, home: Path, generated_guide: Path) -> None:
    if ctx.dry_run:
        return
    ctx.guard((7,))
    text = generated_guide.read_text(encoding="utf-8")
    if text.splitlines().count("@RTK.md") != 1 or "ax work" not in text or "ax status" not in text:
        raise CutoverError("generated_guide_invalid")
    old = home / ".claude/CLAUDE.md"
    if not (ctx.archive_dir / "claude-md/CLAUDE.md").is_file():
        raise CutoverError("original_not_saved")
    if "@RTK.md" not in old.read_text(encoding="utf-8").splitlines():
        raise CutoverError("old_rtk_missing")
    old.write_text(text, encoding="utf-8")


def remove_old_entry(ctx: Context, home: Path, *, new_entry_usable: bool) -> None:
    if ctx.dry_run:
        return
    ctx.guard((7,))
    if new_entry_usable is not True:
        raise CutoverError("entry_not_usable")
    inventory = _storage_inventory()
    settings_file = home / ".claude/settings.json"
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    cleaned = without_pending_guard(settings)
    if cleaned != settings:
        if not (ctx.archive_dir / "settings/settings.json").is_file():
            raise CutoverError("original_not_saved")
        settings_file.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # The copies made in stage 1 stay separate from the moved originals.
    for relative in ("rules/roles", "rules/topics/models-routing.md", "hub/ROLE.md"):
        source = home / ".claude" / relative
        if not source.exists():
            continue
        if source.is_symlink():
            raise CutoverError("entry_symlink")
        dest = ctx.archive_dir / "rules" / "removed" / relative
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if dest.exists():
            raise CutoverError("entry_archive_conflict")
        if source.stat().st_dev != dest.parent.stat().st_dev:
            raise CutoverError("entry_cross_filesystem")
        source.rename(dest)
    # storage.md has one precise replacement; accept only the known old or new wording.
    storage = home / ".claude/rules/topics/storage.md"
    lines = storage.read_text(encoding="utf-8").splitlines(keepends=True)
    new_line = inventory["replacement_line"]
    matches = [i for i, line in enumerate(lines) if line.startswith("- 削除しない。") and inventory["legacy_archive_marker"] in line]
    if len(matches) == 1 and not any(inventory["legacy_host_marker"] in line for i, line in enumerate(lines) if i != matches[0]):
        lines[matches[0]] = new_line
        storage.write_text("".join(lines), encoding="utf-8")
    elif new_line not in lines or any(inventory["legacy_host_marker"] in line for line in lines):
        raise CutoverError("storage_wording_changed")

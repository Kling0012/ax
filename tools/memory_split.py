#!/usr/bin/env python3
"""Split a MEMORY index using F-24; no writes without --apply.

python3 -I tools/memory_split.py MEMORY.md table.txt OUTPUT.md \
    [--backup ORIGINAL_COPY.md] [--apply] [--json]

Only this index and its archive index are written. Individual notes stay put.
The caller verifies cutover authorization, records cutover.state and README,
creates a ToDo on todo_required, and continues with other memory indexes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Mapping, Sequence


MOVE = "移す"
KEEP = "残す"
CHECK_NAMES = (
    "moved+kept=N0",
    "moved lines were in original",
    "no moved line remains",
    "kept lines unchanged",
)
LINK = re.compile(r"\]\(([^)]+)\)")
OUTPUT_NAME = re.compile(r"MEMORY-old-workflow-index-\d{4}-\d{2}-\d{2}(?:-[2-9]\d*|-1\d+)?\.md\Z")


@dataclass(frozen=True)
class SplitPlan:
    original: tuple[str, ...]
    moved: tuple[str, ...]
    kept: tuple[str, ...]
    remaining: tuple[str, ...]
    unlisted: tuple[str, ...]
    nolink: tuple[int, ...]


def load_table(path: Path) -> dict[str, str]:
    """Read the two-column table extracted from F-24 (all projects)."""
    verdicts: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 2 or fields[1] not in (MOVE, KEEP):
            raise ValueError(f"table row {number}: expected filename and 移す|残す")
        name, verdict = fields
        if name in verdicts and verdicts[name] != verdict:
            raise ValueError(f"table row {number}: conflicting verdict")
        verdicts[name] = verdict
    return verdicts


def make_plan(original: str, verdicts: Mapping[str, str]) -> SplitPlan:
    """Partition actual rows, preserving every byte after UTF-8 round-trip."""
    lines = tuple(original.splitlines(keepends=True))
    moved, kept, remaining, unlisted, nolink = [], [], [], [], []
    for number, line in enumerate(lines, 1):
        if not line.startswith("- ["):
            remaining.append(line)
            continue
        match = LINK.search(line)
        name = match.group(1) if match else None
        if name is None:
            nolink.append(number)
        elif not name.startswith("archive/") and name not in verdicts:
            unlisted.append(name)
        if name is not None and not name.startswith("archive/") and verdicts.get(name) == MOVE:
            moved.append(line)
        else:
            kept.append(line)
            remaining.append(line)
    return SplitPlan(lines, tuple(moved), tuple(kept), tuple(remaining), tuple(unlisted), tuple(nolink))


def verify_plan(plan: SplitPlan) -> list[dict]:
    """Check the four S-24.3 invariants using actual rows, not table counts."""
    items = [line for line in plan.original if line.startswith("- [")]
    moved_counts = Counter(plan.moved)
    original_counts = Counter(items)
    values = (
        len(plan.moved) + len(plan.kept) == len(items),
        not bool(moved_counts - original_counts),
        not any(line in moved_counts for line in plan.remaining),
        [line for line in plan.remaining if line.startswith("- [")] == list(plan.kept)
        and [line for line in plan.remaining if not line.startswith("- [")]
        == [line for line in plan.original if not line.startswith("- [")],
    )
    return [{"name": name, "ok": ok} for name, ok in zip(CHECK_NAMES, values)]


def utc_day() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def available_output(memory: Path, day: str | None = None) -> Path:
    """Select -2, -3, ... without overwriting; creation still uses O_EXCL."""
    day = day or utc_day()
    dt.date.fromisoformat(day)
    root = Path(memory).parent / "archive"
    base = f"MEMORY-old-workflow-index-{day}"
    candidate = root / f"{base}.md"
    suffix = 2
    while os.path.lexists(candidate):
        candidate = root / f"{base}-{suffix}.md"
        suffix += 1
    return candidate


def _with_archive_link(plan: SplitPlan, output: Path) -> str:
    link = (
        f"- [旧ワークフローの索引](archive/{output.name}) — AX への切り替えで外した"
        "旧ワークフロー（task-launcher、worker-pool、issue-automation、ToDo 台帳など）の索引\n"
    )
    lines = list(plan.remaining)
    for index, line in enumerate(lines):
        if line.rstrip("\r\n") == "## Archived":
            # Insert inside the section, even when another heading follows it.
            end = next((i for i in range(index + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
            prefix = "".join(lines[:end])
            separator = "" if prefix.endswith("\n") else "\n"
            return prefix + separator + link + "".join(lines[end:])
    body = "".join(lines)
    separator = "" if not body or body.endswith("\n") else "\n"
    return body + separator + "\n## Archived\n" + link


def _result(memory: Path, output: Path) -> dict:
    return {
        "schema_version": 1,
        "subject": "memory-split",
        "memory": str(memory),
        "output": str(output),
        "status": "planned",
        "exit_code": 0,
        "counts": {"n0": 0, "moved": 0, "kept": 0, "unlisted": 0, "nolink": 0},
        "checks": [],
        "unlisted": [],
        "nolink": [],
        "backup_verified": False,
        "todo_required": False,
        "reason": None,
        "output_created": False,
        "staging_file": None,
    }


def _blocked(result: dict, reason: str, code: int = 1) -> dict:
    result.update(status="blocked", exit_code=code, todo_required=True, reason=reason)
    return result


def split_memory(
    memory: Path,
    table: Path,
    output: Path,
    *,
    backup: Path | None = None,
    apply: bool = False,
    day: str | None = None,
) -> dict:
    """Return a per-file result; do not send events or create cards here.

    --backup must be a distinct readable regular copy matching the current
    pre-edit index. A no-op never requires a new backup or creates an output.
    Staging uses TMPDIR and must be on the source filesystem for os.replace.
    On an I/O failure staging/archive files are retained, never removed.
    """
    memory, table, output = Path(memory), Path(table), Path(output)
    result = _result(memory, output)
    try:
        if memory.is_symlink() or not memory.is_file():
            return _blocked(result, "memory-not-regular")
        source_stat = memory.stat()
        original_bytes = memory.read_bytes()
        original = original_bytes.decode("utf-8")
        plan = make_plan(original, load_table(table))
        result["counts"] = {
            "n0": sum(line.startswith("- [") for line in plan.original),
            "moved": len(plan.moved),
            "kept": len(plan.kept),
            "unlisted": len(plan.unlisted),
            "nolink": len(plan.nolink),
        }
        result["checks"] = verify_plan(plan)
        result["unlisted"] = list(plan.unlisted)
        result["nolink"] = list(plan.nolink)
        if not all(check["ok"] for check in result["checks"]):
            return _blocked(result, "verification-failed")
        if not plan.moved:
            result["status"] = "noop"
            return result

        body = _with_archive_link(plan, output)
        # The generated link is excluded from the kept-row comparison. Any
        # change in an original row's line ending is still a failed check.
        body_lines = body.splitlines(keepends=True)
        generated = f"- [旧ワークフローの索引](archive/{output.name})"
        kept_after = [line for line in body_lines if line.startswith("- [") and not line.startswith(generated)]
        if kept_after != list(plan.kept):
            result["checks"][3]["ok"] = False
            return _blocked(result, "verification-failed")

        if backup is not None:
            backup = Path(backup)
            if backup.is_symlink() or not backup.is_file() or backup.samefile(memory):
                return _blocked(result, "backup-not-independent")
            if backup.read_bytes() != original_bytes:
                return _blocked(result, "backup-mismatch")
            result["backup_verified"] = True
        if not apply:
            return result
        # Explicit destination collisions have a distinct documented exit code.
        if os.path.lexists(output):
            return _blocked(result, "output-exists", 2)
        if not result["backup_verified"]:
            return _blocked(result, "backup-required")
        if output.parent.is_symlink() or output.parent.resolve() != (memory.parent / "archive").resolve():
            return _blocked(result, "output-directory")
        if not OUTPUT_NAME.fullmatch(output.name):
            return _blocked(result, "output-name")
        temp_root = os.environ.get("TMPDIR")
        if not temp_root:
            return _blocked(result, "tmpdir-required")
        if Path(temp_root).stat().st_dev != source_stat.st_dev:
            return _blocked(result, "atomic-replace-cross-device")
        day = day or utc_day()
        dt.date.fromisoformat(day)
        archived = (
            f"AX 切り替え時に移した旧ワークフローの索引（{day}）\n\n" + "".join(plan.moved)
        ).encode("utf-8")
        fd, staged = tempfile.mkstemp(prefix="ax-memory-", suffix=".ax-new", dir=temp_root)
        result["staging_file"] = staged
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(source_stat.st_mode))
            stream.write(body.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if not _source_unchanged(memory, original_bytes, source_stat):
            return _blocked(result, "source-changed")
        output.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
        try:
            out_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _blocked(result, "output-exists", 2)
        result["output_created"] = True
        with os.fdopen(out_fd, "wb") as stream:
            stream.write(archived)
            stream.flush()
            os.fsync(stream.fileno())
        if output.read_bytes() != archived:
            return _blocked(result, "archive-verification-failed")
        if not _source_unchanged(memory, original_bytes, source_stat):
            return _blocked(result, "source-changed")
        os.replace(staged, memory)
        result.update(status="applied", staging_file=None)
        return result
    except (OSError, ValueError, UnicodeError) as exc:
        # No contents or exception messages: those may contain sensitive data.
        return _blocked(result, f"io-or-input:{type(exc).__name__}")


def _source_unchanged(memory: Path, original: bytes, before: os.stat_result) -> bool:
    if memory.is_symlink():
        return False
    after = memory.stat()
    return (
        (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
        == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
        and memory.read_bytes() == original
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("memory", type=Path)
    parser.add_argument("table", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    result = split_memory(args.memory, args.table, args.output, backup=args.backup, apply=args.apply)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        counts = result["counts"]
        print(" ".join(f"{key}={counts[key]}" for key in ("n0", "moved", "kept", "unlisted", "nolink")))
        for name in result["unlisted"]:
            print("unlisted:", name)
        for check in result["checks"]:
            print("OK  " if check["ok"] else "FAIL", check["name"])
        print(result["status"], result["reason"] or "")
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())

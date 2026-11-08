"""Offline file trees for T-151; every writable path is below tmp_path."""
from dataclasses import dataclass
from pathlib import Path

from tools.memory_split import available_output, load_table


DAY = "2026-11-07"
TABLE = Path(__file__).resolve().parents[4] / "docs/cutover/memory/f24-table.txt"


@dataclass(frozen=True)
class MemoryCopy:
    memory: Path
    backup: Path
    output: Path


def memory_copy(root: Path, content: bytes, project: str = "project") -> MemoryCopy:
    memory = root / "projects" / project / "memory/MEMORY.md"
    backup = root / "cutover/memory-index" / project / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    backup.parent.mkdir(parents=True)
    memory.write_bytes(content)
    backup.write_bytes(content)
    return MemoryCopy(memory, backup, available_output(memory, DAY))


def reference_content() -> bytes:
    """F-24's first 119 filenames with neutral fake titles and five headings."""
    starts = {
        "ax-rebuild-decision.md": "Workflow & workers",
        "search-first-diff-only.md": "Rules & infra",
        "adhd-friendly-reading.md": "UI & writing",
        "balance-delegated.md": "Dreamforge",
        "gentest-no-unrequested-runs.md": "Other projects",
    }
    lines = []
    for number, name in enumerate(list(load_table(TABLE))[:119], 1):
        if name in starts:
            lines.extend([f"## {starts[name]}\n", "\n"])
        lines.append(f"- [fixture {number}]({name}) — fixture text {number}\n")
    return "".join(lines).encode("utf-8")

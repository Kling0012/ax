#!/usr/bin/env python3
"""Render S-22.9 commander guides using only the Python standard library.

The targets file uses JSON syntax, a YAML 1.2 subset. Public integration API:
    guide_version(body) -> str
    render_guide(target='agents', *, commander_dir=...) -> str
    load_bundle(commander_dir=...) -> GuideBundle
No service, environment configuration, or model is consulted.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMMANDER_DIR = ROOT / "commander"
DEFAULT_OUTPUT_DIR = ROOT / "dist" / "commander"
STAMP = "<!-- ax-guide-version: {version}; generated from commander/GUIDE.md; do not edit -->"


def normalize_lf(body: str) -> str:
    return body.replace("\r\n", "\n").replace("\r", "\n")


def guide_version(body: str) -> str:
    """Hash the complete LF-normalized body, including its final newline."""
    return hashlib.sha256(normalize_lf(body).encode("utf-8")).hexdigest()[:12]


def validate_source(body: str) -> None:
    """Reject external rule imports, personal paths, and common secret shapes."""
    patterns = (
        r"~\/\.claude(?:/|\b)",
        r"/home/",
        r"(?m)^\s*@\S+",
        r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----",
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        r"\bAKIA[A-Z0-9]{16}\b",
    )
    if not body.strip():
        raise ValueError("empty commander guide")
    if any(re.search(pattern, body) for pattern in patterns):
        # Never echo the matched value, which may be a credential.
        raise ValueError("guide contains an external rule path, import, or secret shape")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate targets key")
        result[key] = value
    return result


def load_targets(path: Path) -> list[dict]:
    """Read the documented JSON-compatible YAML table without PyYAML."""
    try:
        table = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("targets.yaml must use JSON-compatible YAML syntax") from exc
    if not isinstance(table, dict) or not isinstance(table.get("targets"), list):
        raise ValueError("targets must be an array")
    names, files = set(), set()
    for row in table["targets"]:
        if not isinstance(row, dict):
            raise ValueError("target must be an object")
        name, filename, fmt = (row.get(key) for key in ("name", "file", "format"))
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name == "stdout":
            raise ValueError("invalid target name")
        if not isinstance(filename, str) or not filename or "\\" in filename:
            raise ValueError("invalid target filename")
        relative = PurePosixPath(filename)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in filename.split("/")):
            raise ValueError("target filename must be a relative path without traversal")
        if name in names or filename in files:
            raise ValueError("duplicate target name or filename")
        names.add(name)
        files.add(filename)
        if fmt not in ("plain", "mdc"):
            raise NotImplementedError("unsupported target format")
        for field in ("prepend", "append"):
            lines = row.get(field, [])
            if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
                raise ValueError("target additions must be arrays of strings")
            if name != "claude" and any(re.search(r"(?m)^\s*@\S+", line) for line in lines):
                raise ValueError("imports are allowed only in the claude target")
        if fmt == "mdc":
            frontmatter = row.get("frontmatter")
            if (not isinstance(frontmatter, dict)
                    or not isinstance(frontmatter.get("description"), str)
                    or frontmatter.get("alwaysApply") is not True):
                raise ValueError("mdc requires description and alwaysApply: true")
    if "agents" not in names:
        raise ValueError("agents target is required for stdout")
    return table["targets"]


def _render(body: str, row: dict, version: str) -> str:
    chunks = [STAMP.format(version=version) + "\n"]
    if row["format"] == "mdc":
        description = json.dumps(row["frontmatter"]["description"], ensure_ascii=False)
        chunks.insert(0, f"---\ndescription: {description}\nalwaysApply: true\n---\n")
    if row.get("prepend"):
        chunks.append("\n".join(normalize_lf(line) for line in row["prepend"]) + "\n\n")
    chunks.append(body)
    if row.get("append"):
        chunks.append(("\n" if body.endswith("\n") else "\n\n")
                      + "\n".join(normalize_lf(line) for line in row["append"]) + "\n")
    return "".join(chunks)


@dataclass(frozen=True)
class GuideBundle:
    guide_version: str
    targets: dict[str, str]
    files: dict[str, str]

    @property
    def agents(self) -> str:
        return self.targets["agents"]

    def render(self, target: str = "agents") -> str:
        return self.targets["agents" if target == "stdout" else target]


def load_bundle(commander_dir: Path = DEFAULT_COMMANDER_DIR) -> GuideBundle:
    """Read one source snapshot; expose its version and agents body together."""
    commander_dir = Path(commander_dir)
    body = normalize_lf((commander_dir / "GUIDE.md").read_text(encoding="utf-8"))
    validate_source(body)
    rows = load_targets(commander_dir / "targets.yaml")
    version = guide_version(body)
    targets = {row["name"]: _render(body, row, version) for row in rows}
    files = {row["file"]: targets[row["name"]] for row in rows}
    return GuideBundle(version, targets, files)


def render_guide(target: str = "agents", *, commander_dir: Path = DEFAULT_COMMANDER_DIR) -> str:
    """Return the complete generated text; stdout is an alias of agents."""
    return load_bundle(commander_dir).render(target)


def write_bundle(bundle: GuideBundle, output_dir: Path, *, check: bool = False) -> list[str]:
    """Write only declared files, or report differences without modifying any file."""
    output_dir = Path(output_dir).resolve()
    paths = {name: output_dir / name for name in bundle.files}
    # Preflight every path before creating any output. Do not follow symlinks out.
    for path in paths.values():
        if path.is_symlink() or not path.resolve().is_relative_to(output_dir):
            raise ValueError("output path escapes output directory")
    mismatches = []
    for name, expected in bundle.files.items():
        path = paths[name]
        if check:
            if not path.is_file() or path.read_bytes() != expected.encode("utf-8"):
                mismatches.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected.encode("utf-8"))
    return mismatches


def validate_help(body: str, help_text: str, *, allow_missing: frozenset | set = frozenset()) -> None:
    """Check guide command paths and options against ax --help text (T-206e).

    ``help_text`` is caller-supplied help output; this function never invokes
    a process. It may be the concatenation of ``ax --help`` and every
    ``ax <command> --help``, matching how argparse splits help across
    subparsers. ``allow_missing`` is an explicit, caller-chosen set of guide
    entries that may be absent (for example, options of operations not yet
    implemented by their owner); the default allows nothing.
    """
    commands, options = set(), set()
    for snippet in re.findall(r"`([^`\n]+)`", body):
        options.update(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", snippet))
        if not snippet.startswith("ax "):
            continue
        command = ["ax"]
        for token in shlex.split(snippet)[1:]:
            if not re.fullmatch(r"[a-z][a-z0-9-]*", token):
                break
            command.append(token)
        if len(command) > 1:
            commands.add(" ".join(command))
    missing = [item for item in sorted(commands | options - set(allow_missing))
               if not re.search(r"(?<![\w-])" + re.escape(item).replace(r"\ ", r"\s+")
                                + r"(?![\w-])", help_text)]
    if missing:
        raise ValueError("guide entries missing from ax --help: " + ", ".join(missing))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commander-dir", type=Path, default=DEFAULT_COMMANDER_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--check", action="store_true", help="compare existing output without writing")
    parser.add_argument("--target", help="print one target without writing files (stdout aliases agents)")
    parser.add_argument("--version", action="store_true", help="print guide_version without writing files")
    parser.add_argument("--help-file", type=Path, help="also check commands against captured ax --help")
    args = parser.parse_args(argv)
    if args.check and (args.target or args.version):
        parser.error("--check cannot be combined with --target or --version")
    try:
        bundle = load_bundle(args.commander_dir)
        if args.help_file:
            body = (args.commander_dir / "GUIDE.md").read_text(encoding="utf-8")
            validate_help(body, args.help_file.read_text(encoding="utf-8"))
        if args.version:
            print(bundle.guide_version)
        elif args.target:
            sys.stdout.write(bundle.render(args.target))
        else:
            mismatches = write_bundle(bundle, args.output_dir, check=args.check)
            if mismatches:
                print("guide output differs: " + ", ".join(mismatches), file=sys.stderr)
                return 1
    except (OSError, ValueError, KeyError, NotImplementedError) as exc:
        # Do not print source text, parser excerpts, or credential matches.
        print(f"guide generation failed ({type(exc).__name__})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

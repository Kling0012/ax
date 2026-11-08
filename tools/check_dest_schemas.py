#!/usr/bin/env python3
"""Check S-6.22 destination schemas and the complete layered intake registry.

This command only reads files. It never retrieves a schema over the network.
The CLI fails on incomplete contracts or integration gaps; row validation keeps
unrelated valid destinations available to callers implementing fail-soft reload.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012

DRAFT = "https://json-schema.org/draft/2020-12/schema"
BASE = "https://ax.invalid/schemas/"
DESTINATIONS = frozenset("""
control.command work.request subloop.create core.change ledger.answer secret.push
emergency change.request change.cancel_merge permission.change work.stop work.cancel
work.resume ledger.complete ledger.park schedule.update life_state.update goal.update
commitment.update decision.record claim.op seat.add seat.enroll_start seat.tier
seat.revoke seat.lock seat.unlock go.confirm approval.grant approval.deny approval.withdraw
""".split())


def _offline(uri: str):
    raise NoSuchResource(ref=uri)


def _registry(root: Path) -> Registry:
    resources = []
    for path in sorted((root / "schemas").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resources.append((BASE + path.name, Resource.from_contents(doc, default_specification=DRAFT202012)))
    return Registry(retrieve=_offline).with_resources(resources)


def validate_registry_rows(rows: list, root: Path) -> tuple[dict[str, dict], list[str]]:
    """Validate rows independently, applying the existing last-definition rule.

    An invalid replacement removes that destination. It does not silently
    resurrect an older definition, or remove other valid destinations.
    """
    root = Path(root)
    document = json.loads((root / "schemas/intake-destinations.schema.json").read_text(encoding="utf-8"))
    row_schema = {"$id": document["$id"], **document["items"]}
    validator = Draft202012Validator(row_schema, registry=_registry(root))
    entries: dict[str, dict] = {}
    errors: list[str] = []
    for index, row in enumerate(rows):
        name = row.get("name") if isinstance(row, dict) else None
        label = name if isinstance(name, str) else f"row[{index}]"
        if isinstance(name, str):
            entries.pop(name, None)
        if not isinstance(row, dict) or row.get("tier") not in {"read", "operate", "irreversible"} or not row.get("via"):
            errors.append(f"{label}: tier_via_missing")
            continue
        faults = sorted(validator.iter_errors(row), key=lambda e: list(map(str, e.path)))
        if faults:
            errors.append(f"{label}: registry_schema_failed ({faults[0].validator})")
            continue
        if row.get("irreversible", row["tier"] == "irreversible") != (row["tier"] == "irreversible"):
            errors.append(f"{label}: irreversible_tier_mismatch")
            continue
        if "hq" not in row["via"] and "hq" in row["allowed_from"]:
            errors.append(f"{label}: allowed_from_via_mismatch")
            continue
        if any(v != "hq" for v in row["via"]) and "commander" not in row["allowed_from"]:
            errors.append(f"{label}: allowed_from_via_mismatch")
            continue
        if "hq" in row["via"] and "hq" not in row["allowed_from"]:
            errors.append(f"{label}: allowed_from_via_mismatch")
            continue
        entries[name] = dict(row)
    return entries, errors


def load_registry(root: Path) -> tuple[dict[str, dict], list[str]]:
    """Read the main file followed by every .d fragment in filename order."""
    root = Path(root)
    config = root / "config"
    paths = [config / "intake-destinations.yaml"]
    paths.extend(sorted((config / "intake-destinations.d").glob("*.yaml")))
    rows: list = []
    errors: list[str] = []
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            errors.append(f"{path.relative_to(root)}: registry_file_unreadable")
            continue
        if not isinstance(data, list):
            errors.append(f"{path.relative_to(root)}: registry_not_array")
            continue
        rows.extend(data)
    entries, row_errors = validate_registry_rows(rows, root)
    return entries, errors + row_errors


def _references(value):
    if isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"]
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _check_refs(doc: dict, root: Path) -> None:
    """Resolve only ax.invalid schema files below schemas/, never remote URIs."""
    for ref in _references(doc):
        uri, fragment = urldefrag(urljoin(doc["$id"], ref))
        if not uri.startswith(BASE):
            raise ValueError("external_schema_reference")
        relative = unquote(uri[len(BASE):])
        path = (root / "schemas" / relative).resolve()
        if not path.is_relative_to((root / "schemas").resolve()):
            raise ValueError("schema_reference_outside_root")
        target = doc if uri == doc["$id"] else json.loads(path.read_text(encoding="utf-8"))
        if fragment:
            if not fragment.startswith("/"):
                raise ValueError("unsupported_schema_anchor")
            for part in unquote(fragment)[1:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                target = target[int(key)] if isinstance(target, list) else target[key]


def _change_maximum(go: dict) -> int:
    alternatives = go["properties"]["changes"]["oneOf"]
    arrays = [part for part in alternatives if part.get("type") == "array"]
    if len(arrays) != 1:
        raise ValueError("changes_array_contract_missing")
    return arrays[0]["items"]["maximum"]


def _facts_change_maximum(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    section = re.search(r"^### F-15\.2\b[^\n]*\n(.*?)(?=^## |^### |\Z)", text, re.M | re.S)
    if not section:
        raise ValueError("F-15.2_section_missing")
    numbers = [int(n) for n in re.findall(r"^\|\s*(\d+)\s*\|", section.group(1), re.M)]
    if not numbers or sorted(numbers) != list(range(1, max(numbers) + 1)):
        raise ValueError("F-15.2_numbering_invalid")
    return max(numbers)


def check(root: Path, *, facts_path: Path | None = None) -> list[str]:
    """Return all schema/registry integration faults, without mutating files."""
    root = Path(root)
    entries, errors = load_registry(root)
    directory = root / "schemas/dest"
    files = {path.stem: path for path in directory.glob("*.json")}
    for label, actual in (("registry", set(entries)), ("schemas", set(files))):
        for name in sorted(DESTINATIONS - actual):
            errors.append(f"{label}: missing_destination:{name}")
        for name in sorted(actual - DESTINATIONS):
            errors.append(f"{label}: extra_destination:{name}")
    for name in sorted(set(entries) - set(files)):
        errors.append(f"{name}: destination_schema_missing")
    for name in sorted(set(files) - set(entries)):
        errors.append(f"{name}: destination_schema_extra")
    for name, entry in sorted(entries.items()):
        if entry["schema"] != f"schemas/dest/{name}.json":
            errors.append(f"{name}: noncanonical_schema_reference")
    for name, path in sorted(files.items()):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(doc)
            if doc.get("$schema") != DRAFT:
                errors.append(f"{name}: draft_2020_12_required")
            if doc.get("$id") != f"{BASE}dest/{name}.json":
                errors.append(f"{name}: schema_id_mismatch")
            if doc.get("type") != "object" or doc.get("additionalProperties") is not False:
                errors.append(f"{name}: closed_object_required")
            if doc.get("x-ax-unresolved"):
                errors.append(f"{name}: payload_contract_unresolved")
            _check_refs(doc, root)
            if name == "go.confirm" and facts_path is not None:
                if _change_maximum(doc) != _facts_change_maximum(Path(facts_path)):
                    errors.append("go.confirm: F-15.2_changes_maximum_mismatch")
        except Exception as exc:
            # Keep checking other files and do not expose untrusted schema text.
            errors.append(f"{name}: schema_invalid:{type(exc).__name__}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--facts", type=Path, help="Optional FACTS.md for F-15.2 change-number consistency")
    args = parser.parse_args(argv)
    try:
        errors = check(args.root, facts_path=args.facts)
    except Exception as exc:
        errors = [f"destination_check_failed:{type(exc).__name__}"]
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False))
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())

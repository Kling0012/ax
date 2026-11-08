"""Read actual branch files and stage registry/compose together; never deploy."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from .policy import Rejected


def plugin_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise Rejected(400, "bad_request")
    return value


def confined(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = root / relative
    # Reject links even if they currently resolve within the candidate tree.
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise Rejected(422, "candidate_path_invalid")
    if not path.resolve().is_relative_to(root):
        raise Rejected(422, "candidate_path_invalid")
    return path


def read_candidate(root: Path, ident: str) -> tuple[dict, dict]:
    ident = plugin_id(ident)
    manifest = yaml.safe_load(confined(root, f"plugins/subloop/{ident}/manifest.yaml").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("id") != ident or manifest.get("kind") != "subloop":
        raise Rejected(422, "candidate_manifest_invalid")
    graph_name = manifest.get("graph")
    if not isinstance(graph_name, str) or Path(graph_name).is_absolute():
        raise Rejected(422, "candidate_path_invalid")
    base = confined(root, f"plugins/subloop/{ident}")
    graph_file = confined(base, graph_name)
    graph = yaml.safe_load(graph_file.read_text(encoding="utf-8"))
    if not isinstance(graph, dict):
        raise Rejected(422, "candidate_graph_invalid")
    return manifest, graph


def attribution(record: dict) -> dict:
    return {key: record[key] for key in ("created_by", "quote", "approval_id") if record.get(key) is not None}


def commit_message(record: dict) -> str:
    return f"Register subloop {record['plugin_id']}\n\n" + "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in attribution(record).items())


def stage_registration(root: Path, ident: str, manifest: dict, *, template_root: Path) -> list[str]:
    """Called only for an intake-owned branch, after both validation passes.

    Keep a registry fragment as well as registry.yaml so build_registry --check
    remains consistent. The common accept_branch adapter commits these paths
    with the candidate and only then requests deploy.
    """
    from tools.build_registry import build

    ident = plugin_id(ident)
    fragments = confined(root, "plugins/registry.d")
    fragments.mkdir(parents=True, exist_ok=True)
    rel_fragment = f"plugins/registry.d/sl-{ident}.yaml"
    entry = {"id": ident, "kind": "subloop", "path": f"plugins/subloop/{ident}/manifest.yaml"}
    existing = []
    for path in fragments.glob("*.yaml"):
        doc = yaml.safe_load(confined(root, str(path.relative_to(root))).read_text(encoding="utf-8")) or {}
        for candidate in doc.get("plugins", []):
            if candidate.get("id") == ident:
                if path.name != f"sl-{ident}.yaml" or candidate != entry:
                    raise Rejected(409, "subloop_exists")
            existing.append(candidate)
    compose = (template_root / "compose.sl.yaml.tmpl").read_text(encoding="utf-8").replace("@@ID@@", ident)
    document = yaml.safe_load(compose)
    services = document.get("services", {})
    if set(services) != {f"sl-{ident}"}:
        raise Rejected(422, "compose_invalid")
    service = services[f"sl-{ident}"]
    if "ports" in service or "build" in service:
        raise Rejected(422, "compose_invalid")
    if service.get("image") != "ax/runtime:${AX_TAG}":
        raise Rejected(422, "compose_invalid")
    # Include only names; no secret value is ever read here.
    service["secrets"] = list(dict.fromkeys([*service.get("secrets", []), *manifest["secrets"]]))
    fragment = confined(root, rel_fragment)
    fragment.write_text(yaml.safe_dump({"plugins": [entry]}, sort_keys=False), encoding="utf-8")
    registry, errors, _ = build(root)
    if errors:
        raise Rejected(422, "registry_invalid")
    rel_compose = f"compose.d/sl-{ident}.yaml"
    target = confined(root, rel_compose)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    confined(root, "plugins/registry.yaml").write_text(registry, encoding="utf-8")
    return [f"plugins/subloop/{ident}", rel_fragment, "plugins/registry.yaml", rel_compose]

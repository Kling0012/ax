"""plugins/registry.yaml と manifest の読み込み（S-4.1、S-4.2、D-4.1）。

起動時に registry.yaml を読み、載っているプラグインだけを登録する。各 manifest を Schema で検証し、
壊れていればそのプラグインだけを登録しない（他は登録する。fail-soft）。サブループのグラフ検査は
`tools/ax_graph_check.py`（B7）の `errors_for_body(body)` を読む。検査不能なら対象だけを登録しない。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from axcommon import schemas, timeutil

from .graphcheck import errors_for_graph

log = logging.getLogger("core.control.registry")

DEFAULT_REGISTRY = "/ax/plugins/registry.yaml"


def registry_path() -> Path:
    return Path(os.environ.get("AX_REGISTRY", DEFAULT_REGISTRY))


def load_registry(path: str | Path | None = None) -> list[dict]:
    import yaml

    p = Path(path) if path else registry_path()
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if errs := schemas.errors("registry", doc):
        raise ValueError(f"registry.yaml が Schema に合わない: {'; '.join(errs[:3])}")
    return doc["plugins"]


def manifest_errors(doc) -> list[str]:
    from .service import CORE_COMPONENTS, knob_check

    errs = schemas.errors("manifest", doc)
    if errs:
        return errs
    if doc["id"] in CORE_COMPONENTS:
        errs.append("中核の部品はプラグインとして登録できない")
    for name, knob in (doc.get("knobs") or {}).items():
        if err := knob_check(knob, knob["default"]):
            errs.append(f"knob {name} の既定値: {err}")
        if knob.get("min") is not None and knob.get("max") is not None and knob["min"] > knob["max"]:
            errs.append(f"knob {name} の min が max より大きい")
    if doc.get("kind") == "subloop" and not doc.get("graph"):
        errs.append("subloop は graph が必須")
    if doc.get("accepts_items") and "inbox_max" not in (doc.get("knobs") or {}):
        errs.append("accepts_items は inbox_max の宣言が必須")
    return errs


def load_manifest(root: Path, entry: dict) -> tuple[dict | None, list[str]]:
    """manifest を読んで Schema 検査する。(manifest, errors)。"""
    import yaml

    root = root.resolve()
    path = Path(entry["path"])
    mpath = (root / path).resolve()
    parts = path.parts
    if len(parts) == 3 and parts[0] == "runtime" and parts[1] not in (".", "..") and parts[2] == "manifest.yaml":
        allowed = root / "runtime" / parts[1]
    else:
        allowed = root / "plugins"
    try:
        mpath.relative_to(allowed)
    except ValueError:
        return None, ["manifest のパスが許可されたルートの外にある"]
    try:
        doc = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
    except OSError as e:
        return None, [f"manifest が読めない: {e.strerror}"]
    except yaml.YAMLError as e:
        return None, [f"manifest が YAML でない: {type(e).__name__}"]
    errs = manifest_errors(doc)
    if not errs and doc.get("id") != entry["id"]:
        errs = [f"manifest の id（{doc.get('id')}）が登録簿（{entry['id']}）と違う"]
    if not errs and doc.get("kind") != entry["kind"]:
        errs = [f"manifest の kind（{doc.get('kind')}）が登録簿（{entry['kind']}）と違う"]
    if not errs and doc.get("kind") == "subloop":
        if (mpath.parent / doc["graph"]).resolve().parent != mpath.parent:
            return None, ["subloop の manifest または graph のパスが不正"]
        gpath = mpath.parent / doc["graph"]
        try:
            graph = yaml.safe_load(gpath.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            graph = None
        gerrs = errors_for_graph({"graph": graph, "manifest": doc}, root=root, subloop_dir=mpath.parent) if graph is not None else ["graph ファイルが読めない"]
        errs += [f"graph: {e}" for e in gerrs]
    return (doc if not errs else None), errs


def register_all(store, *, root: Path | None = None, registry_file: str | Path | None = None, preserve_ids=()) -> dict:
    """registry.yaml の全項目を登録する。{registered: [...], rejected: [(id, [理由])]}。"""
    root = root or Path(os.environ.get("AX_HOME", "/ax"))
    entries = load_registry(registry_file)
    now = timeutil.now_ts()
    out: dict[str, Any] = {"registered": [], "rejected": []}
    for old in store.list_plugins():
        if old["id"] not in preserve_ids and old["id"] not in {e["id"] for e in entries}:
            old.update(status="rejected", note="登録簿から外れた", revision=old["revision"] + 1)
            store.invalidate_run_target(old["id"])
            store.upsert_plugin(old)
    for entry in entries:
        if entry["id"] in preserve_ids:
            out["rejected"].append((entry["id"], ["limits synchronization unavailable"]))
            continue
        previous = store.get_plugin(entry["id"])
        revision = previous["revision"] + 1 if previous else 1
        marks = previous["user_set_knobs"] if previous else {}
        manifest, errs = load_manifest(root, entry)
        if manifest is None:
            store.invalidate_run_target(entry["id"])
            out["rejected"].append((entry["id"], errs))
            store.upsert_plugin({"id": entry["id"], "kind": entry["kind"], "path": entry["path"],
                                 "manifest": {}, "revision": revision, "user_set_knobs": marks, "status": "rejected",
                                 "note": "; ".join(errs[:3]), "registered_at": now})
            continue
        store.upsert_plugin({"id": entry["id"], "kind": entry["kind"], "path": entry["path"],
                             "manifest": manifest, "revision": revision, "user_set_knobs": marks, "status": "registered", "registered_at": now})
        out["registered"].append(entry["id"])
    return out

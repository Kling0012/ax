"""b6a の試験の共通の組み立て。ネットワークとサービスを使わない。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.resources.store import Store as ResourcesStore  # noqa: E402
import importlib; JobsStore = importlib.import_module('plugins.resource.worker-pool.jobs').JobsStore  # noqa: E402


def resources_store(tmp_path: Path) -> ResourcesStore:
    return ResourcesStore(str(tmp_path / "resources.sqlite"))


def jobs_store(tmp_path: Path) -> JobsStore:
    return JobsStore(str(tmp_path / "worker-pool.sqlite"))


def seed_models(store, rows: list[dict] | None = None) -> list[dict]:
    rows = rows if rows is not None else default_models()
    store.seed([dict(r) for r in rows])
    return store.list(limit=2000)


def default_models() -> list[dict]:
    return [
        {"id": "our-free-model/a-free", "kind": "model", "family": "free", "tier": "free", "billing": "flat",
         "level": "L1", "status": "active", "prefix_cache": "unknown", "cap": 1},
        {"id": "our-free-model/b-free", "kind": "model", "family": "free", "tier": "free", "billing": "flat",
         "level": "L2", "status": "active", "prefix_cache": "unknown", "cap": 1},
        {"id": "zhipu-cp/glm-5.3-flash", "kind": "model", "family": "glm-flash", "tier": "flash", "billing": "flat",
         "level": "L3", "vision": True, "status": "active", "prefix_cache": "unknown", "cap": 3},
        {"id": "zcode/GLM-5.3", "kind": "model", "family": "glm", "tier": "standard", "billing": "flat",
         "level": "L3", "status": "active", "prefix_cache": "unknown", "cap": 3, "pricing": {"input": 1.2, "output": 3.6}},
        {"id": "codex/gpt-6.1-sol", "kind": "model", "family": "gpt", "tier": "last_resort", "billing": "flat",
         "level": "L4", "status": "active", "prefix_cache": "unknown", "cap": 4},
        {"id": "codex/gpt-6.1-terra", "kind": "model", "family": "gpt", "tier": "last_resort", "billing": "flat",
         "level": "L4", "status": "active", "prefix_cache": "unknown", "cap": 4},
        {"id": "claude/claude-sonnet-5-5", "kind": "model", "family": "claude", "tier": "last_resort", "billing": "flat",
         "level": "L4", "vision": True, "status": "active", "prefix_cache": "unknown", "cap": 1},
        {"id": "claude/claude-opus-4-8", "kind": "model", "family": "claude", "tier": "last_resort", "billing": "flat",
         "level": "L4", "status": "active", "prefix_cache": "unknown", "cap": 1},
        {"id": "mistral/metered-m", "kind": "model", "family": "mistral", "tier": "standard", "billing": "metered",
         "level": "L3", "status": "active", "prefix_cache": "unknown", "cap": 2},
        {"id": "rog/metered-x", "kind": "model", "family": "rog", "tier": "standard", "billing": "metered",
         "level": "L3", "status": "active", "prefix_cache": "unknown", "cap": 2,
         "pricing": {"input": 0.5, "output": 1.0}},
    ]

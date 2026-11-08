"""Validation of worker-pool settings against the live resource catalog."""
from pathlib import Path

import yaml

from axcommon.policy import TIER_ORDER


def validate_settings(name, value, catalog):
    rows = catalog["resources"]
    if not isinstance(rows, list) or len(rows) >= 500:
        raise ValueError("incomplete resource catalog")
    if any(not isinstance(r, dict) or r.get("kind") != "model" or
           not isinstance(r.get("id"), str) or not isinstance(r.get("family"), str)
           for r in rows):
        raise ValueError("invalid resource catalog")
    models = {r["id"] for r in rows}
    families = {r["family"] for r in rows if r["family"]}
    with (Path(__file__).resolve().parents[2] / "config" / "roles.yaml").open(encoding="utf-8") as f:
        roles = set(yaml.safe_load(f)["roles"])
    if not isinstance(value, dict):
        return "mapping required"
    if name == "role_candidates":
        for role, entries in value.items():
            if role not in roles or not isinstance(entries, list):
                return "unknown role or invalid list"
            if any(not isinstance(v, str) or v not in models | set(TIER_ORDER) for v in entries):
                return "unknown model or tier"
            if len(set(entries)) != len(entries):
                return "duplicate candidate"
    elif name in ("family_cap", "model_cap"):
        valid = families if name == "family_cap" else models
        if any(k not in valid or type(v) is not int or v < 0 for k, v in value.items()):
            return "unknown key or invalid cap"
    elif name == "model_descriptions":
        if any(k not in models or not isinstance(v, str) for k, v in value.items()):
            return "unknown model or invalid description"
    return None

"""schemas/ext/b9/ の自己完結のスキーマを直接検査する（b3 の extschemas と同じ形）。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parent.parent.parent / "schemas" / "ext" / "b9"


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    return json.loads((EXT_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def errors(name: str, instance) -> list[str]:
    import jsonschema

    try:
        v = jsonschema.Draft202012Validator(load(name))
    except (OSError, ValueError) as e:
        return [f"拡張スキーマ {name} が読めない: {e}"]
    return sorted(f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in v.iter_errors(instance))

"""schemas/ext/b3/ の台帳の拡張スキーマの読み込み（目標・約束・予定・判断の記録）。

S-9.4・S-9.6 は本文の形を文で書くだけで、schemas/ の JSON Schema が無いため、この束が schemas/ext/b3/ に
自分の分を置く（PLAN.json の conventions）。$ref を持たない自己完結のスキーマで、ここで直接検査する。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_EXT_DIR = Path(__file__).resolve().parent.parent.parent / "schemas" / "ext" / "b3"


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    return json.loads((_EXT_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def _validator(name: str):
    import jsonschema

    return jsonschema.Draft202012Validator(_load(name))


def errors(name: str, instance) -> list[str]:
    try:
        v = _validator(name)
    except (OSError, ValueError) as e:
        return [f"拡張スキーマ {name} が読めない: {e}"]
    return sorted(f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
                  for e in v.iter_errors(instance))


def validate_item(kind: str, item) -> list[str]:
    """kind は goal / promise / schedule。日時は実在する RFC 3339 を受ける。"""
    from axcommon import timeutil

    found = errors(kind, item)
    if found:
        return found
    for key in ("created_at", "updated_at", "due_at", "start_at", "end_at"):
        if key in item:
            try:
                timeutil.parse_ts(item[key])
            except (TypeError, ValueError):
                found.append(f"{key}: RFC 3339 でない")
    return found


def validate_decision(d) -> list[str]:
    return errors("decision", d)

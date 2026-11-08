"""tests/b1 の補助。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXT_DIR = ROOT / "schemas" / "ext" / "b1"


def ext_errors(name: str, instance) -> list[str]:
    """schemas/ext/b1/<name>.schema.json で検査する（axcommon.schemas は直下の schemas/ だけを読むため、ここで登録簿を作る）。"""
    import jsonschema

    docs = {}
    for p in list((ROOT / "schemas").glob("*.json")) + list(EXT_DIR.glob("*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        docs[doc["$id"]] = doc
    schema = json.loads((EXT_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    try:
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        reg = Registry().with_resources((u, Resource.from_contents(d, default_specification=DRAFT202012)) for u, d in docs.items())
        v = jsonschema.Draft202012Validator(schema, registry=reg)
    except ImportError:  # 古い jsonschema
        v = jsonschema.Draft202012Validator(schema, resolver=jsonschema.RefResolver(base_uri=schema["$id"], referrer=schema, store=docs))
    return sorted(e.message for e in v.iter_errors(instance))

"""翻訳 JSON を作る偽部品。"""
from __future__ import annotations

import json


def translation_json(**values) -> bytes:
    return json.dumps(values, ensure_ascii=False).encode("utf-8")

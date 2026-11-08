"""事象の要約（D-9.1）。LLM へ渡す物は、data を短縮し、秘密検査（S-15.3）を通した写しだけ。"""
from __future__ import annotations

import re
from typing import Any, Callable

Redact = Callable[[Any], Any]

_SENS = ("public", "private", "sensitive")


def identity(x):
    return x


def shorten(value: Any, *, str_max: int = 200, depth: int = 3, keys_max: int = 12, items_max: int = 8) -> Any:
    """data の短縮。長い文字列は切り、深い入れ子と多い項目は落とす。"""
    if isinstance(value, str):
        return value if len(value) <= str_max else value[:str_max] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth <= 0:
        return "…"
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= keys_max:
                out["…"] = f"他 {len(value) - keys_max} 項目"
                break
            out[str(k)] = shorten(v, str_max=str_max, depth=depth - 1, keys_max=keys_max, items_max=items_max)
        return out
    if isinstance(value, (list, tuple)):
        items = [shorten(v, str_max=str_max, depth=depth - 1, keys_max=keys_max, items_max=items_max) for v in list(value)[:items_max]]
        if len(value) > items_max:
            items.append(f"…他 {len(value) - items_max} 件")
        return items
    return str(value)[:str_max]


def sensitivity_of(ev: dict) -> str:
    """事象の情報区分。data.sensitivity があればそれ、なければ private（事象の本体に区分の欄が無いため。DECISIONS）。
    不正な値は最も厳しい sensitive として扱う（閉じる側）。"""
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    s = data.get("sensitivity")
    if s is None:
        return "private"
    return s if s in _SENS else "sensitive"


def summarize_event(ev: dict, *, str_max: int = 200, redact: Redact = identity) -> dict:
    # Redact full values before shortening; truncation would hide exact indexed matches.
    ev = redact(ev)
    src = ev.get("source") if isinstance(ev.get("source"), dict) else {}
    out = {
        "id": ev.get("id"), "seq": ev.get("seq"), "kind": ev.get("kind"), "ts": ev.get("ts"), "subject": ev.get("subject"),
        "component": src.get("component"), "project": ev.get("project"), "subloop": ev.get("subloop"),
        "data": shorten(ev.get("data") or {}, str_max=str_max),
    }
    return redact({k: v for k, v in out.items() if v is not None})


_SPACE = re.compile(r"\s+")


def one_line(text: str, n: int = 160) -> str:
    t = _SPACE.sub(" ", text or "").strip()
    return t if len(t) <= n else t[:n] + "…"

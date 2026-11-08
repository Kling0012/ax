"""送る記録の検査（S-14.2）: 必須項目と、秘密・個人情報（S-15.3）。

必須項目: project、subloop（無ければ core）、host、source（事象 ID）、kind。content も要る。
必須項目のない記録は拒否する。秘密か個人情報に当たった記録は拒否する（置き換えて送らない）。拒否の応答に、当たった値は入れない（種別と件数だけ）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from axcommon import ids
from axcommon.redact import Redactor, summarize

KINDS = ("decision", "observation", "issue", "result", "important_artifact", "preference")
REQUIRED = ("project", "host", "source", "kind", "content")
ALLOWED = set(REQUIRED) | {"subloop", "supersedes", "valid_from", "refs"}
REF_RE = re.compile(r"^(event|todo):[A-Za-z0-9_-]+$")
MAX_CONTENT = 20000
TEXT_FIELDS = ("project", "subloop", "host", "source", "kind", "content", "supersedes", "valid_from")


@dataclass
class Rejection:
    code: str          # missing_fields / bad_request / secret_detected
    message: str       # 利用者に見せてよい短い説明（値を含まない）
    fields: list[str] = field(default_factory=list)
    categories: dict[str, int] = field(default_factory=dict)


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def normalize(req) -> tuple[dict | None, Rejection | None]:
    """必須項目と形を検査し、subloop の既定（core）を入れた写しを返す。秘密検査はしない（check_secrets）。"""
    if not isinstance(req, dict):
        return None, Rejection("bad_request", "本文が object でない")
    extra = sorted(set(req) - ALLOWED)
    if extra:
        return None, Rejection("bad_request", "未知の項目がある: " + ", ".join(extra), fields=extra)
    missing = [f for f in REQUIRED if _blank(req.get(f))]
    if missing:
        return None, Rejection("missing_fields", "必須項目がない: " + ", ".join(missing), fields=missing)
    for f in TEXT_FIELDS:
        if f in req and req[f] is not None and not isinstance(req[f], str):
            return None, Rejection("bad_request", f"{f} は文字列", fields=[f])
    out = {k: v for k, v in req.items() if v is not None}
    if _blank(out.get("subloop")):
        out["subloop"] = "core"
    if out["kind"] not in KINDS:
        return None, Rejection("bad_request", "kind が不正（" + "、".join(KINDS) + "）", fields=["kind"])
    if not ids.is_ulid(out["source"]):
        return None, Rejection("bad_request", "source は事象 ID（ULID）", fields=["source"])
    if len(out["content"]) > MAX_CONTENT:
        return None, Rejection("bad_request", f"content は {MAX_CONTENT} 文字まで", fields=["content"])
    refs = out.get("refs", [])
    if not isinstance(refs, list) or len(refs) > 20 or any(not isinstance(r, str) or not REF_RE.match(r) for r in refs):
        return None, Rejection("bad_request", "refs は event:<ID> か todo:<ID> の一覧（20 件まで）", fields=["refs"])
    return out, None


def check_secrets(rec: dict, redactor: Redactor | None) -> Rejection | None:
    """全ての文字列の項目を S-15.3 で検査する。当たったら拒否（種別と件数だけを返す）。"""
    if redactor is None:
        return None
    hits = []
    for f in TEXT_FIELDS:
        v = rec.get(f)
        if isinstance(v, str):
            hits += redactor.scan(v)
    for r in rec.get("refs", []):
        hits += redactor.scan(r)
    if hits:
        return Rejection("secret_detected", "秘密か個人情報に当たる記録は送れない", categories=summarize(hits))
    return None

"""指示の検証（S-6.5）と行き先への受け渡し（D-5 の処理の流れの骨格）。

処理の流れ: 受付 → `instruction.received` → 検証（S-6.5）→ `idempotency_key` の照合（S-6.16）→
衝突の検出（S-6.17。b12b3 の差し込み口）→ 承認が要る行き先は承認待ち（S-6.10。b12b2 の差し込み口）→
行き先の handler へ POST → 結果を保存 → `instruction.routed` か `instruction.rejected`。

拒否の reason は文書にある物を使う: `forged_authority`（S-6.8）、`tier_insufficient`（S-6.9）、
`receptacle_not_allowed`（S-6.9）、`idempotency_key_required`・`idempotency_mismatch`（S-6.16）、
`conflict`（S-6.17）、`user_only_card`（S-6.9）。文書に名の無い物は実装者の決め
（`unknown_destination`、`payload_schema`、`expired`、`not_allowed_from`）→ DECISIONS.md。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from axcommon import schemas, timeutil

from . import vias
from .registry import Registry, payload_contract, unresolved_contract

log = logging.getLogger("axintake.validation")


@dataclass(frozen=True)
class Rejection:
    status: int
    reason: str
    message: str = ""


def _payload_errors(schema: dict, payload: Any) -> list[str]:
    """payload を Schema（dict そのもの）で検査する。人に読める文字列の一覧。"""
    import jsonschema

    v = jsonschema.Draft202012Validator(schema)
    out = []
    for e in v.iter_errors(payload):
        path = "/".join(str(p) for p in e.absolute_path)
        out.append(f"{path or '(root)'}: {e.message}")
    return sorted(out)


def validate(registry: Registry, body: Any, ident: vias.Ident) -> Rejection | None:
    """S-6.5 の検証。通れば None、拒否なら Rejection。承諾待ち（202）も Rejection の形で返す。"""
    if not isinstance(body, dict):
        return Rejection(422, "bad_instruction", "指示の本文が JSON のオブジェクトでない")

    # 指示の形（schemas/instruction.schema.json。from は受け口が決めた値で埋めて検査する）
    check = dict(body)
    check.setdefault("from", ident.from_)
    errs = schemas.errors("instruction", check)
    if errs:
        return Rejection(422, "bad_instruction", "; ".join(errs[:3]))

    # 本文の申告と確かめた値の照合（S-6.2、S-6.8）
    forged = vias.assert_body_matches(ident, body)
    if forged:
        return Rejection(403, forged, "本文の from が資格から決めた値と違う" if forged == "forged_authority" else "資格の偽装")

    # 1. destination が登録簿にある
    dest = registry.get(body.get("destination"))
    if dest is None:
        return Rejection(404, "unknown_destination", "登録簿にない行き先")
    if unresolved_contract(dest):
        return Rejection(501, "contract_unresolved", "行き先の handler が未実装")

    # 2. from が allowed_from に含まれる
    if ident.from_ not in dest["allowed_from"]:
        return Rejection(403, "not_allowed_from", "この行き先への指示元ではない")

    # 6/7. 受け口の資格が有効で、行き先の via にその受け口があり、tier が席の段以下
    if ident.via not in dest["via"]:
        return Rejection(403, "receptacle_not_allowed", "この受け口から出せる行き先ではない")
    if not vias.tier_sufficient(ident.max_tier, dest["tier"]):
        return Rejection(403, "tier_insufficient", "段が足りない")

    # 3. payload が行き先の JSON Schema に合う（Schema がまだ無い行き先は、形の検査だけ。registry.py）
    payload = body.get("payload")
    schema_name = dest.get("_schema_name")
    payload_schema = None
    if schema_name:
        try:
            payload_schema = schemas.load(schema_name)
        except Exception:  # noqa: BLE001  登録時に読めた Schema が読めなくなったとき
            return Rejection(500, "payload_schema_unavailable", "行き先の Schema が読めない")
    declared = set((payload_schema or {}).get("properties") or {})
    reserved = vias.payload_reserved_keys(payload, declared)
    if reserved:
        return Rejection(403, "forged_authority", "payload に資格の名前を持つ項目がある: " + ",".join(reserved))
    if payload_schema is not None:
        payload_schema = payload_contract(dest, payload_schema)
        if payload_schema.get("x-ax-unresolved"):
            return Rejection(501, "contract_unresolved", "行き先の handler が未実装")
        errs = _payload_errors(payload_schema, payload)
        if errs:
            return Rejection(422, "payload_schema", "; ".join(errs[:3]))

    # 4. 戻しにくい操作（tier: irreversible）
    if dest["tier"] == "irreversible":
        if body.get("confirm_irreversible") is True and ident.from_ == "hq":
            pass  # 承認の digest の照合は承認の保存（b12b2）の差し込み口が担う
        elif vias.tier_sufficient(ident.max_tier, "irreversible"):
            # 拒否せず承認待ちにする（S-6.9。HTTP 202、承認の保存は b12b2 の差し込み口）
            return Rejection(202, "approval_required", "承認待ちにした")
        else:
            return Rejection(403, "tier_insufficient", "段が足りない")

    # 5. expires_at が過去でない
    exp = body.get("expires_at")
    if isinstance(exp, str):
        try:
            if timeutil.parse_ts(exp) <= timeutil.now():
                return Rejection(422, "expired", "期限が過ぎている")
        except ValueError:
            return Rejection(422, "bad_instruction", "expires_at が RFC 3339 でない")

    # 8. 運用の段以上の指示は idempotency_key を持つ（S-6.16）
    if dest["tier"] in ("operate", "irreversible") and not body.get("idempotency_key"):
        return Rejection(422, "idempotency_key_required", "運用の段以上の指示には idempotency_key が要る")

    return None

"""受け口（via）と資格の骨格（S-6.2、S-6.8、F-9.2）。

`from` と身元（`principal`）は受け口が確かめた事実から決める。本文の `from` は参考の値で、資格から
決めた値と違えば拒否する（`instruction.rejected`、reason `forged_authority`）。追加の権限トークンは
作らない。サブループ・ワーカー・管理役などの内部コンテナは、どの受け口にも届かず、資格も持たない
（S-15.5。compose の網分けは compose.d/b5.yaml）。

B5 が作る受け口は `local`（commander ソケット。接続元 uid の照合）と `hq`（専用ソケット。uid が
`ax-hq`）の骨格。`ssh`（席のソケット）と `tailnet`（gate の中継）の身元の決め方は後続の束（b12b1）が
`register_via_factory` の差し込み口へ足す。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("axintake.vias")

TIER_RANK = {"read": 0, "operate": 1, "irreversible": 2}

# S-6.2: 外部の文章や payload が持っていても権限にならない名前（Schema にない場所なら拒否）
RESERVED_PAYLOAD_FIELDS = ("from", "principal", "issued_by", "confirm_irreversible", "approved_via", "tier")

# S-6.8 の表の固定値
PRINCIPAL_LOCAL = "local-operator"
PRINCIPAL_HQ = "user"
LOCAL_MAX_TIER = "operate"
HQ_MAX_TIER = "irreversible"


@dataclass(frozen=True)
class Ident:
    """受け口が確かめた値（S-6.2 の via、principal を決める材料）。"""

    via: str                      # local | ssh | tailnet | hq
    from_: str                    # commander | hq
    principal: str                # 席の ID か固定の身元
    max_tier: str = "read"        # この受け口・席の段の上限
    detail: str = ""              # 確かめた事実の短い記録（秘密の値は入れない）


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def local_principal() -> str:
    """via=local の身元（S-6.8: 既定の local-operator）。表示の名前は環境変数で替えられる。"""
    return os.environ.get("AX_LOCAL_PRINCIPAL") or PRINCIPAL_LOCAL


def local_ident(uid: int, *, allowed_uid: int | None = None) -> Ident | None:
    """commander ソケットの受け口。接続元の uid が司令塔のユーザー（F-9.2 の AX_UID）であること。"""
    allowed = allowed_uid if allowed_uid is not None else _int_env("AX_UID")
    if allowed is None:
        allowed = os.geteuid()
    if uid != allowed:
        return None
    return Ident("local", "commander", local_principal(), LOCAL_MAX_TIER, f"uid={uid}")


def hq_ident(uid: int, *, allowed_uid: int | None = None) -> Ident | None:
    """hq 専用ソケットの受け口。接続元の uid が ax-hq（AX_HQ_UID）であること（S-6.8）。

    AX_HQ_UID が無い（B10 より前）のときは口を開けない（F-18.6）。
    """
    allowed = allowed_uid if allowed_uid is not None else _int_env("AX_HQ_UID")
    if allowed is None:
        return None
    if uid != allowed:
        return None
    return Ident("hq", "hq", PRINCIPAL_HQ, HQ_MAX_TIER, f"uid={uid}")


# 後続の束（b12b1 の ssh・tailnet）の差し込み口: {via 名: (accepted_uid から Ident を作る関数)}
EXTRA_IDENT_FACTORIES: dict[str, Callable[[int], Ident | None]] = {}


def register_via_factory(via: str, factory: Callable[[int], Ident | None]) -> None:
    EXTRA_IDENT_FACTORIES[via] = factory


def ident_for(via: str, uid: int) -> Ident | None:
    if via == "local":
        return local_ident(uid)
    if via == "hq":
        return hq_ident(uid)
    factory = EXTRA_IDENT_FACTORIES.get(via)
    if factory is None:
        return None
    try:
        return factory(uid)
    except Exception:  # noqa: BLE001  席の照合の失敗はその接続だけを断つ
        log.exception("受け口 %s の身元の判定に失敗", via)
        return None


def assert_body_matches(ident: Ident, body: Any) -> str | None:
    """本文の申告と確かめた値の照合（S-6.2）。違えば拒否の reason を返す。"""
    if isinstance(body, dict):
        ref = body.get("from")
        if ref is not None and ref != ident.from_:
            return "forged_authority"
        if body.get("confirm_irreversible") is True and ident.from_ != "hq":
            # confirm_irreversible は from=hq で承認 ID が付いた指示だけが true を持てる（S-6.5）
            return "forged_authority"
    return None


def payload_reserved_keys(payload: Any, declared: set[str] | None = None) -> list[str]:
    """payload の中の、Schema にない場所にある資格の名前（S-6.2）。空なら問題なし。"""
    if not isinstance(payload, dict):
        return []
    declared = declared or set()
    return [k for k in RESERVED_PAYLOAD_FIELDS if k in payload and k not in declared]


def tier_sufficient(seat_tier: str, dest_tier: str) -> bool:
    return TIER_RANK.get(seat_tier, -1) >= TIER_RANK.get(dest_tier, 99)

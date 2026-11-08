"""情報区分の照合（S-15.1、D-14）。worker-pool、管理役、グラフ検査が同じ関数を使う。

allowed(sensitivity, target) -> bool
  target はモデル（resource: kind=model、tier）か実行環境（environment: data_classes、adapter）の dict。
  public    : 登録済みのすべてのモデル、すべての実行環境。
  private   : 登録済みのモデル。data_classes が private を許す実行環境。cloud-sandbox（box、Instabox）には渡さない。
  sensitive : tier が standard 以上（standard、last_resort）の登録済みモデルだけ。環境は data_classes が sensitive を許す物
              （cloud-sandbox は不可）。
「登録済み」は target が resource の登録簿の項目であること（呼び出し側が登録簿から引いた dict を渡す）。None や空は不可。
学習利用（trains_on_input）は条件にしない（S-15.1 の利用者の決定）。秘密と個人情報の除外は axcommon.redact（S-15.3）が別に行う。
"""
from __future__ import annotations

from typing import Any

TIER_ORDER = {"free": 0, "flash": 1, "standard": 2, "last_resort": 3}
SENSITIVITIES = ("public", "private", "sensitive")


def is_environment(target: dict) -> bool:
    return target.get("kind") == "environment" or "adapter" in target or "data_classes" in target


def allowed(sensitivity: str, target: dict[str, Any] | None) -> bool:
    if sensitivity not in SENSITIVITIES:
        raise ValueError(f"情報区分が不正: {sensitivity!r}")
    if not target:
        return False  # 登録されていない物へは送らない
    if is_environment(target):
        if target.get("adapter") == "cloud-sandbox" and sensitivity != "public":
            return False
        return sensitivity in (target.get("data_classes") or []) or sensitivity == "public"
    # モデル
    if sensitivity == "public" or sensitivity == "private":
        return True
    tier = target.get("tier")
    return tier in TIER_ORDER and TIER_ORDER[tier] >= TIER_ORDER["standard"]


def most_restrictive(*classes: str) -> str:
    """複数の区分のうち最も厳しい物（ジョブの入力が混ざるとき）。"""
    return max(classes, key=SENSITIVITIES.index)

"""資源台帳の登録簿の読み込み（S-8.2、F-7.3、D-8）。

config/models.yaml（profile の models_file）を初回だけ読み込み、.resources 表へ移す。
以後の状態は DB が正で、ファイルは初期値だけ（D-8）。
config/models.example.yaml はこのリポジトリで、秘密の値を含まない見本。

登録の 1 件の形は schemas/resource.schema.json（S-8.2、F-7.3）:
  id、kind（model / environment）、family、tier（free / flash / standard / last_resort）、
  level（L1〜L4）、billing（metered / flat）、vision、runner、cap、prefix_cache
  （on / off / unknown。既定 unknown）、status（active / degraded / excluded / retired）、
  trains_on_input（記録だけ。判断に使わない）、stats、description、pricing（unit、input、output、
  fetched_at、出所）。環境は kind: environment で、秘密の可否などを持つ（S-25.2）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from axcommon import schemas

MODELS_FILENAME = "models.yaml"

# F-7.3 の表。下に挙がった順が tier の順（free → flash → standard → last_resort）。
TIER_ORDER = {"free": 0, "flash": 1, "standard": 2, "last_resort": 3}
LEVEL_ORDER = {"L1": 0, "L2": 1, "L3": 2, "L4": 3}
KINDS = ("model", "environment")
STATUSES = ("active", "degraded", "excluded", "retired")
BILLINGS = ("metered", "flat")
PREFIX_VALUES = ("on", "off", "unknown")
TRAINS_VALUES = ("no_train", "train", "unknown")


class CatalogError(ValueError):
    """登録簿の読み込みに失敗した（1 件でも書かず、元の表を保つ）。"""


def models_path(env: dict | None = None) -> Path:
    """実ファイルの置き場。見本を実際の資源台帳に取り込まない。"""
    e = os.environ if env is None else env
    root = Path(__file__).resolve().parent.parent.parent
    if e.get("AX_MODELS_FILE"):
        return Path(e["AX_MODELS_FILE"])
    return root / "config" / MODELS_FILENAME


def load_models_file(path: str | os.PathLike) -> list[dict]:
    import yaml

    p = Path(path)
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise CatalogError(f"{p}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise CatalogError(f"{p}: YAML でない") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), list):
        raise CatalogError(f"{p}: models の一覧が無い")
    out: list[dict] = []
    for i, item in enumerate(doc["models"]):
        if not isinstance(item, dict):
            raise CatalogError(f"{p}: {i} 件目がオブジェクトでない")
        errs = schemas.errors("resource", item)
        if errs:
            raise CatalogError(f"{p}: {item.get('id', i)!r}: " + "; ".join(errs[:3]))
        if item.get("supported_runners") and item.get("runner") not in item["supported_runners"]:
            raise CatalogError(f"{p}: {item.get('id', i)!r}: default runner must be supported")
        out.append(dict(item))
    ids = [m["id"] for m in out]
    if len(set(ids)) != len(ids):
        raise CatalogError(f"{p}: id の重複がある")
    return out


def validate_resource(item: dict) -> list[str]:
    """1 件の登録が S-8.2 の決まりに合うか。問題の文字列の一覧を返す（空なら合格）。"""
    bad: list[str] = []
    if item.get("kind") not in KINDS:
        bad.append("kind は model か environment")
    if "tier" in item and item["tier"] not in TIER_ORDER:
        bad.append("tier が不正")
    if "level" in item and item["level"] not in LEVEL_ORDER:
        bad.append("level が不正")
    if "billing" in item and item["billing"] not in BILLINGS:
        bad.append("billing が不正")
    if "prefix_cache" in item and item["prefix_cache"] not in PREFIX_VALUES:
        bad.append("prefix_cache が不正")
    if "status" in item and item["status"] not in STATUSES:
        bad.append("status が不正")
    if "trains_on_input" in item and item["trains_on_input"] not in TRAINS_VALUES:
        bad.append("trains_on_input が不正")
    if "cap" in item and (not isinstance(item["cap"], int) or isinstance(item["cap"], bool) or item["cap"] < 0):
        bad.append("cap は 0 以上の整数")
    return bad

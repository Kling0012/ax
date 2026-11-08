"""能力評価と昇格・降格（S-8.5、S-17、R-N22〜R-N26）。

合否は verify.cmd・関門の検証で決める。system.expected、利用制限、quota、接続障害、保留は
能力上の合否に数えない（S-8.5）。カウントは counts_as_failure が yes / no の試行だけ。

- 新しい無料モデルは L1 から始める。同じ段階で promote_after 回連続合格なら 1 段上げ、
  demote_after 回連続不合格なら 1 段下げる。GLM と GPT も実績に従い段階を上下させる。
  役割の最低段階と情報区分の安全線は維持する。
- 無料モデルの成功率が free_min_runs 以上の対象試行で free_min_success 未満なら能力成績の除外。
  直近の能力上の失敗から worker_recent_failure_min 分は通常候補から除外する。
  別モデルの試行は分母に入れない（1 モデルの試行で数える）。
- 利用制限・接続障害は回復まで別理由で除外し、回復時刻不明なら exclude_rate_limit_min 後に再評価。
- 実作業の promotion_share を上限として昇格候補を割り当てる。昇格だけの試験は作らない。
  未昇格の無料モデルに昇格試行を理由として実装作業を割り当てない（R-N26）。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from axcommon import timeutil

from .catalog import LEVEL_ORDER, TIER_ORDER
from .store import Store

LEVELS = ("L1", "L2", "L3", "L4")


def _parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return timeutil.parse_ts(text)
    except ValueError:
        return None


def _get_default(name: str, fallback: Any) -> Any:
    try:
        from axcommon.defaults import Defaults

        return Defaults.load().get(name)
    except Exception:  # noqa: BLE001  既定値が読めなくても止めない（S-17 の既定値）
        return fallback


def knob(name: str, override: Any = None) -> Any:
    """S-17 の既定値。強調値（knob）があればそちらに置き換える（S-4.3 の表）。"""
    if override is not None:
        return override
    return _get_default(name, None)


def _capability_trials(trials: list[dict]) -> list[dict]:
    """能力上の合否だけを順に返す（counts_as_failure が yes / no）。"""
    return [t for t in trials if t.get("counts_as_failure") in ("yes", "no")]


def _consec(trials: list[dict], *, want: str) -> int:
    """want（pass / fail）の連続の長さ。"""
    n = 0
    for t in reversed(trials):
        if t.get("outcome") == want:
            n += 1
        else:
            break
    return n


def evaluate(store: Store, resource_id: str, *, now: str | None = None,
             promote_after: int | None = None, demote_after: int | None = None, stage_only: bool = False) -> dict:
    """今の成績で次の段階を決める。変えないときは new_level を None で返す（呼び出し側が適用する）。"""
    now = now or timeutil.now_ts()
    res = store.get(resource_id)
    if res is None:
        return {"resource_id": resource_id, "new_level": None, "reason": "not_found"}
    pa = promote_after if promote_after is not None else (_get_default("promote_after", 3) or 3)
    da = demote_after if demote_after is not None else (_get_default("demote_after", 2) or 2)
    trials = _capability_trials(store.capability_outcomes(resource_id))
    generation = (res.get("stats") or {}).get("level_generation", 0)
    if stage_only:
        trials = [t for t in trials if t.get("level_generation") == generation]
    cur = res.get("level", "L1")
    idx = LEVELS.index(cur) if cur in LEVELS else 0
    if _consec(trials, want="pass") >= pa and idx < len(LEVELS) - 1:
        return {"resource_id": resource_id, "new_level": LEVELS[idx + 1], "reason": "promote_after"}
    if _consec(trials, want="fail") >= da and idx > 0:
        return {"resource_id": resource_id, "new_level": LEVELS[idx - 1], "reason": "demote_after"}
    return {"resource_id": resource_id, "new_level": None, "reason": "streak_not_met"}


def free_success(store: Store, resource_id: str, *, since: str | None = None) -> dict:
    """無料モデルの成功率（対象試行だけ。別モデルの試行は分母に入れない）。"""
    trials = _capability_trials(store.capability_outcomes(resource_id, since=since))
    if not trials:
        return {"trials": 0, "success_rate": None}
    ok = sum(1 for t in trials if t.get("outcome") == "pass")
    return {"trials": len(trials), "success_rate": ok / len(trials)}


def should_exclude_for_success(store: Store, resource_id: str, *, now: str | None = None,
                               min_success: float | None = None, min_runs: int | None = None) -> dict:
    ms = min_success if min_success is not None else (_get_default("free_min_success", 0.7) or 0.7)
    mr = min_runs if min_runs is not None else (_get_default("free_min_runs", 3) or 3)
    r = free_success(store, resource_id)
    if r["trials"] >= mr and r["success_rate"] is not None and r["success_rate"] < ms:
        return {"exclude": True, "reason": "low_success_rate", **r, "threshold": ms}
    return {"exclude": False, "reason": "ok", **r, "threshold": ms}


def recently_failed(store: Store, resource_id: str, *, now: str | None = None,
                    recent_min: int | None = None) -> dict:
    """直近の能力上の失敗で除外する期間の中か（S-8.5、R-N24）。"""
    now = now or timeutil.now_ts()
    rm = recent_min if recent_min is not None else (_get_default("worker_recent_failure_min", 60) or 60)
    trials = _capability_trials(store.capability_outcomes(resource_id))
    if not trials:
        return {"exclude": False, "reason": "no_failures"}
    last = trials[-1]
    at = _parse_ts(last.get("at"))
    if at is None:
        return {"exclude": False, "reason": "no_timestamp"}
    n = _parse_ts(now)
    if n is None:
        return {"exclude": False, "reason": "bad_now"}
    if last.get("outcome") == "fail" and (n - at) < timedelta(minutes=rm):
        return {"exclude": True, "reason": "recent_failure", "until": timeutil.format_ts(at + timedelta(minutes=rm))}
    return {"exclude": False, "reason": "recovered"}


def rate_limited_exclude(store: Store, resource_id: str, *, now: str | None = None,
                         exclude_min: int | None = None) -> dict:
    """利用制限・接続障害で回復まで別理由で除外する（R-N23）。回復時刻が不明なら exclude_rate_limit_min 後に再評価。"""
    now = now or timeutil.now_ts()
    em = exclude_min if exclude_min is not None else (_get_default("exclude_rate_limit_min", 60) or 60)
    res = store.get(resource_id)
    if res is None:
        return {"exclude": False, "reason": "not_found"}
    trials = (res.get("stats") or {}).get("trials") or []
    if not trials:
        return {"exclude": False, "reason": "no_trials"}
    last = trials[-1]
    # 利用制限・接続障害は counts_as_failure が "no" だが、error_class で区別する
    if last.get("error_class") in ("rate_limit", "quota", "network") or last.get("counts_as_failure") == "hold":
        at = _parse_ts(last.get("at"))
        if at is None:
            return {"exclude": True, "reason": "rate_limit_unknown", "until": None}
        until = at + timedelta(minutes=em)
        n = _parse_ts(now)
        if n is not None and n >= until:
            return {"exclude": False, "reason": "reevaluated_after_exclude_min"}
        return {"exclude": True, "reason": "rate_limit", "until": timeutil.format_ts(until)}
    return {"exclude": False, "reason": "ok"}


__all__ = ["evaluate", "free_success", "should_exclude_for_success", "recently_failed",
           "rate_limited_exclude", "LEVELS", "TIER_ORDER", "LEVEL_ORDER"]

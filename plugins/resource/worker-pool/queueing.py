"""待機行と下位切替の判定（S-8.4、D-7.4、R-N20、R-N21）。

- 最安候補が満枠なら枠を待つ。下位切替は S-17 worker_lower_wait_s 以上待ち、
  許可（knob）と実行可能な受け入れ検証があり、安全線を満たす場合に限る。
  検証できなければ待つ。期限切れはそのジョブだけを外へ出す。
- 役割別待機列は S-17 role_queue_max までとし、超過は skipped(queue_full) とする。
- 下位候補への切り替え後は、その候補が実行していない試行の失敗をその候補の成績に数えない（R-N21）。
  score の切り替えは、切り替えたモデルに帰属しない。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WaitState:
    seen_at: str | None = None
    attempts: int = 0


def queue_depth(pending: list[dict], role: str) -> int:
    return sum(1 for j in pending if j.get("role") == role)


def should_queue(pending: list[dict], role: str, *, role_queue_max: int = 5) -> tuple[bool, str]:
    """役割ごとの待機列の上限。超過は skipped(queue_full)。"""
    if queue_depth(pending, role) >= role_queue_max:
        return False, "queue_full"
    return True, "queued"


def lower_fallback_allowed(*, enabled: bool = True, waited_s: float = 0, wait_required_s: int = 300,
                           has_verify: bool = False, safety_ok: bool = False) -> tuple[bool, str]:
    """下位候補への切り替えの可否（S-8.4 の 4、R-N20）。

    許可・待機時間・検証がそろえば切り替える。検証なしは待つ。
    """
    if not enabled:
        return False, "lower_fallback_disabled"
    if waited_s < wait_required_s:
        return False, "lower_wait_not_met"
    if not has_verify:
        return False, "no_verify"
    if not safety_ok:
        return False, "safety_denied"
    return True, "lower_fallback"


def attribute_failure_to_candidate(*, trial_model: str, candidate: str) -> bool:
    """別モデルの失敗を下位候補の成績に入れない（R-N21）。"""
    return trial_model == candidate

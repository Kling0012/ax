"""配送先ごとの方針（S-22.4「配送枠は配送先ごとの方針とし、旧メールの3件制限を ntfy に持ち込まない」）。

- rate_per_min: 1 分あたりの配送枠。枠を超えた通知は未配送（held）として保持し、枠が空いたら再配送する。
- on_hit: 秘密・個人情報が見つかったとき。mask（伏せ字にして送る）| block（送らない）
- on_unverifiable: 検査できないとき。block（送らない）| send
- retry_base_s / retry_max_s: 失敗した通知の再配送の待ち（S-17 の再試行と同じ形: base × 2^回数、最大 max）
旧メールの 3 件制限の値はどこにも持たない。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import DEST_DISCORD, DEST_NTFY


@dataclass(frozen=True)
class DestinationPolicy:
    name: str
    rate_per_min: int
    on_hit: str = "mask"
    on_unverifiable: str = "block"
    retry_base_s: float = 30.0
    retry_max_s: float = 1800.0

    def retry_wait_s(self, attempts: int) -> float:
        return float(min(self.retry_base_s * (2 ** max(0, attempts - 1)), self.retry_max_s))


# Discord の枠は文書に値が無い。実装者の決めとして 10 件/分（DECISIONS.md）。
DISCORD_RATE_PER_MIN = 10


def default_policies(defaults=None) -> dict[str, DestinationPolicy]:
    rate = 60
    if defaults is not None:
        try:
            rate = int(defaults.get("ntfy_rate_per_min"))
        except Exception:  # noqa: BLE001  config が読めなくても動く
            rate = 60
    base, mx = 30.0, 1800.0
    if defaults is not None:
        try:
            b = defaults.entry("retry_backoff")["default"]
            base, mx = float(b["base_s"]), float(b["max_s"])
        except Exception:  # noqa: BLE001
            pass
    return {
        DEST_NTFY: DestinationPolicy(DEST_NTFY, rate, on_hit="mask", on_unverifiable="block", retry_base_s=base, retry_max_s=mx),
        DEST_DISCORD: DestinationPolicy(DEST_DISCORD, DISCORD_RATE_PER_MIN, on_hit="block", on_unverifiable="block",
                                        retry_base_s=base, retry_max_s=mx),
    }

"""周回を回す（B13）。プラグイン 1 つ = 独立した周回（コンテナ 1 つに当たる）として、同じ事象ループで走らせる。

    stats = await run_rounds({"pa": runner_a, "pb": runner_b}, n=3, timeout_s=5)
    stats["pa"].ok / .failed / .timed_out / .errors

runner は引数なしの非同期の呼び出し可能な物 1 周回ぶん。timeout_s は秒数か {名前: 秒数}（既定 10 秒）。1 つが例外を出す・固まる（timeout_s を超える）・でたらめな値を返しても、
他の runner の周回は同じ回数だけ進む。壊れた runner の失敗は stats に残るだけで、他へ伝わらない。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class RoundStats:
    ok: int = 0
    failed: int = 0
    timed_out: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def rounds(self) -> int:
        return self.ok + self.failed + self.timed_out


async def _one(fn: Callable[[], Awaitable], timeout_s: float, st: RoundStats) -> None:
    try:
        await asyncio.wait_for(fn(), timeout_s)
        st.ok += 1
    except asyncio.TimeoutError:
        st.timed_out += 1
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        st.failed += 1
        st.errors.append(type(e).__name__)


async def run_rounds(runners: dict[str, Callable[[], Awaitable]], n: int = 1, timeout_s: "float | dict[str, float]" = 5.0) -> dict[str, RoundStats]:
    stats = {k: RoundStats() for k in runners}

    async def loop(name: str) -> None:
        for _ in range(n):
            await _one(runners[name], timeout_s.get(name, 10.0) if isinstance(timeout_s, dict) else timeout_s, stats[name])

    await asyncio.gather(*(loop(k) for k in runners))
    return stats

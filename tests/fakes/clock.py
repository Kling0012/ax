"""偽の時計。axcommon.timeutil の時計を差し替え、sleep を実際に待たずに時間を進める。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from axcommon import timeutil


class FakeClock:
    def __init__(self, start: str = "2026-11-05T12:00:00.000Z"):
        self._now = timeutil.parse_ts(start)
        self._mono = 0.0
        self.sleeps: list[float] = []

    # axcommon.timeutil に渡す
    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
        self._mono += seconds

    def sleep(self, seconds: float) -> None:
        """実際には待たず、時計だけ進める。呼ばれた秒数を記録する。"""
        self.sleeps.append(seconds)
        self.advance(seconds)

    async def asleep(self, seconds: float) -> None:
        self.sleep(seconds)

    def install(self) -> "FakeClock":
        timeutil.set_clock(self.now)
        return self

    def uninstall(self) -> None:
        timeutil.set_clock(None)

    def __enter__(self):
        return self.install()

    def __exit__(self, *exc):
        self.uninstall()


UTC = timezone.utc

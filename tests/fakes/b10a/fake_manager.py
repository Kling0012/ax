"""偽の管理役の周回（CycleRunner）。予備役の呼び出しを記録する。"""
from __future__ import annotations

from core.observer.standby.cycle import CycleRequest, CycleResult


class FakeManager:
    def __init__(self, *, ok: bool = True, raises: Exception | None = None):
        self.ok, self.raises = ok, raises
        self.requests: list[CycleRequest] = []

    def run(self, req: CycleRequest) -> CycleResult:
        self.requests.append(req)
        if self.raises is not None:
            raise self.raises
        return CycleResult(ok=self.ok, actions=2 if self.ok else 0, reason=None if self.ok else "bad_output", cycle_id="c1", model="fake-model")

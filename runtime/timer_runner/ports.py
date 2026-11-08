"""期限付きタイマーの接続部（S-7.8、F-18.4）。

control:8081 は timers の取得・実行・状態報告を許可しない。
到達経路と資格が定義されるまで、推測した HTTP API は呼ばない。
定期ジョブはこの窓口から独立して動く。
"""
from __future__ import annotations

from typing import Any

from axcommon.client import ApiClient


class ControlTimerPort:
    def __init__(self, client: ApiClient):
        self.client = client

    def list_timers(self) -> list[dict]:
        raise NotImplementedError("S-7.8/F-18.4: timer-runner の timers 取得経路と資格が未定義")

    def mark(self, timer_id: str, state: str, result: Any) -> None:
        raise NotImplementedError("S-7.8/D-4.4: タイマーの実行状態の報告経路が未定義")

    async def fire(self, timer: dict) -> dict:
        raise NotImplementedError("S-7.8/F-18.4: タイマー action の実行経路と責務が未定義")

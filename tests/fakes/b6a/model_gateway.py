"""偽の提供一覧（B6a の試験用）。

tests/fakes/model_gateway.py の /v1/models の偽と違い、ここは resources の
discovery の入力専用の偽。提供一覧の差（新規・一致・未提供）と取得失敗
（T-46）を再現する。
"""
from __future__ import annotations

import json


class FakeDiscovery:
    def __init__(self, provided: list[str] | None = None):
        self.provided = provided if provided is not None else ["zcode/GLM-5.3", "codex/gpt-6.1-sol"]
        self.fail: str | None = None
        self.calls: list[str] = []

    def fail_http(self, status: int = 503) -> "FakeDiscovery":
        self.fail = f"http_{status}"
        return self

    def down(self) -> "FakeDiscovery":
        self.fail = "down"
        return self

    def transport(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float):
        self.calls.append(url)
        if self.fail == "down":
            raise ConnectionError("fake down")
        if self.fail and self.fail.startswith("http_"):
            return int(self.fail[5:]), {"Content-Type": "application/json"}, b'{"error":"fake"}'
        data = [{"id": mid, "object": "model"} for mid in self.provided]
        return 200, {"Content-Type": "application/json"}, json.dumps({"object": "list", "data": data}).encode("utf-8")

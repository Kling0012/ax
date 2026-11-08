"""偽の Jev（B6a の試験用）。ネットワークを使わない。

  fake = FakeJev()
  fake.set("m1", 0.9)          # 候補ごとの適合の点
  fake.fail_http(500)          # HTTP エラーで「使えない」
  fake.timeout()               # タイムアウトで「使えない」
  transport = fake.transport   # core.resources.jev.call に渡す transport

試験は、Jev の偽の適合で候補が絞れること、Jev 不正時も適格候補から最安を選べること（T-10、T-43）を見る。
"""
from __future__ import annotations

import json


class FakeJev:
    def __init__(self):
        self.calls: list[dict] = []
        self._scores: dict[str, float] = {}
        self._http: int | None = None
        self._timeout = False

    def set(self, model_id: str, score: float) -> "FakeJev":
        self._scores[model_id] = score
        return self

    def fail_http(self, status: int = 500) -> "FakeJev":
        self._http = status
        return self

    def timeout(self) -> "FakeJev":
        self._timeout = True
        return self

    def bad_json(self) -> "FakeJev":
        self._scores = {"__bad_json__": 1.0}
        return self

    def transport(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float):
        doc = json.loads((body or b"{}").decode("utf-8"))
        state = doc.get("state") or {}
        self.calls.append({"method": method, "url": url, "state": state, "questions": doc.get("questions")})
        if self._timeout:
            raise TimeoutError("fake timeout")
        if self._http is not None:
            return self._http, {"Content-Type": "application/json"}, b'{"error":"fake"}'
        if "__bad_json__" in self._scores:
            return 200, {"Content-Type": "application/json"}, b"not json"
        # state の model_id あるいは候補の一覧から、questions["suitable"] の点を作る
        mid = state.get("model_id")
        score = self._scores.get(mid or "", 0.1)
        answers = {"suitable": {"noul": score}}
        return 200, {"Content-Type": "application/json"}, json.dumps({"answers": answers}).encode("utf-8")

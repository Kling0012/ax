"""worker-pool の偽（WorkerPoolClient 互換: submit / job / cancel）。モデルの入口は b0 の FakeModelGateway を使う。

  gw = FakeModelGateway(api_key=None)
  gw.script(Reply('{"actions":[{"type":"noop","reason":"様子見"}]}'), Delay(400, Reply("遅い")), RateLimit())
  pool = FakePool(gw, clock)
  pool.submit({...}) → {"id": "job-1", "state": "queued"}。job(id) は clock.monotonic() が ready_at に届くまで running。
Delay はゲートウェイの時計を進めずに記録だけし（gw.clock は None のこと）、その秒数だけジョブを running に保つ。
故障: pool.down() / pool.up()（submit も job も Unavailable）、pool.stuck_next()（次のジョブは終わらない）、
      pool.reject_next(code)（次の submit を 429 で拒否）、pool.lose_next()（次のジョブは lost）。
記録: pool.jobs（辞書の一覧）、pool.bodies（出された本文）、pool.cancelled（取り消した ID）。
"""
from __future__ import annotations

import json
from typing import Any

from axcommon.client import ApiClient, ApiError, Unavailable


class FakePool:
    def __init__(self, gateway: Any, clock: Any, *, model: str = "zhipu/glm-test"):
        self.gw, self.clock, self.model = gateway, clock, model
        self.api = ApiClient("http://gateway", transport=gateway)
        self.jobs: list[dict] = []
        self.bodies: list[dict] = []
        self.cancelled: list[str] = []
        self._down = False
        self._stuck = 0
        self._reject: list[str] = []
        self._lose = 0

    # ---- 故障 ----
    def down(self):
        self._down = True

    def up(self):
        self._down = False

    def stuck_next(self, n: int = 1):
        self._stuck += n

    def reject_next(self, code: str = "queue_full"):
        self._reject.append(code)

    def lose_next(self, n: int = 1):
        self._lose += n

    # ---- WorkerPoolClient 互換 ----
    def submit(self, body: dict) -> dict:
        if self._down:
            raise Unavailable("ConnectionRefused")
        self.bodies.append(body)
        if self._reject:
            raise ApiError(429, self._reject.pop(0), "拒否")
        jid = f"job-{len(self.jobs) + 1}"
        job: dict[str, Any] = {"id": jid, "body": body, "role": body.get("role"), "state": "queued", "model": None, "text": ""}
        before = len(self.gw.delays)
        if self._stuck:
            self._stuck -= 1
            job.update(ready_at=float("inf"), outcome="ok", text="")
        elif self._lose:
            self._lose -= 1
            job.update(ready_at=self.clock.monotonic(), outcome="lost")
        else:
            try:
                resp = self.api.post("/v1/messages", {"model": self.model, "max_tokens": 4096, "system": "",
                                                      "messages": [{"role": "user", "content": body["prompt"]}]})
                text = "".join(c.get("text", "") for c in resp.get("content", []))
                job.update(outcome="ok", text=text, model=resp.get("model"))
            except (ApiError, Unavailable) as e:
                job.update(outcome="failed", text="", error=type(e).__name__)
            delay = sum(self.gw.delays[before:])
            job["ready_at"] = self.clock.monotonic() + delay
        self.jobs.append(job)
        return {"id": jid, "state": "queued"}

    def job(self, job_id: str) -> dict:
        if self._down:
            raise Unavailable("ConnectionRefused")
        j = next((x for x in self.jobs if x["id"] == job_id), None)
        if j is None:
            raise ApiError(404, "not_found", "ジョブがない")
        if j["id"] in self.cancelled:
            return {"id": j["id"], "state": "cancelled", "role": j["role"]}
        if self.clock.monotonic() < j["ready_at"]:
            return {"id": j["id"], "state": "running", "role": j["role"], "model": j.get("model")}
        if j["outcome"] == "ok":
            return {"id": j["id"], "state": "ok", "role": j["role"], "model": j.get("model") or self.model,
                    "report": {"status": "ready", "text": j["text"]}}
        return {"id": j["id"], "state": "lost" if j["outcome"] == "lost" else "failed", "role": j["role"]}

    def cancel(self, job_id: str) -> dict:
        self.cancelled.append(job_id)
        return {"ok": True}

    # ---- 試験の補助 ----
    @property
    def last_prompt(self) -> str:
        return self.bodies[-1]["prompt"] if self.bodies else ""


def decision(*actions: dict) -> str:
    """S-10.3 の形の返答の本文（先頭に説明の文を付ける。最後の JSON が使われる）。"""
    return "判断した。\n" + json.dumps({"actions": list(actions)}, ensure_ascii=False)

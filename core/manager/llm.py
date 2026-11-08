"""モデルの呼び出し（D-9.1、F-7.2）。worker-pool の POST /v1/jobs（役割 manager、runner chat-json）だけを使う。

- モデルの入口にも鍵にも触れない。profile の model_gateway は読まない。GPT と Claude は選べない（選ぶのは worker-pool。S-8.3、S-10.3）。
- 1 回の判断の時間は manager_decision_s まで。超えたらジョブを取り消し、DecisionTimeout（呼び出し側が事象を台帳へ出して次へ進む。D-9.2）。
- 待つ処理は必ず期限を持つ（S-2.2）。時計と sleep は差し替えられる（偽の時計で待たずに試す）。
- worker-pool の ジョブの状態: queued/launching/submitted/running/stalled は待つ。ok は本文を返す。failed/timed_out/cancelled/lost/rejected は LLMUnavailable。
- 返答の本文は GET /v1/jobs/{id} の report.text（chat-json の応答の本文）。最後の JSON オブジェクトを取り出す（parse_json）。
"""
from __future__ import annotations

import copy
import json
import math
import time
from dataclasses import dataclass
from typing import Callable

from axcommon.client import ApiError, Unavailable, WorkerPoolClient

TERMINAL = ("ok", "failed", "timed_out", "cancelled", "lost", "rejected")
WAITING = ("queued", "launching", "submitted", "running", "stalled")


class LLMError(Exception):
    pass


class DecisionTimeout(LLMError):
    """manager_decision_s を超えた（D-9.2）。"""


class LLMUnavailable(LLMError):
    """モデルを使えない（worker-pool が落ちている、ジョブが失敗・拒否された）。同じ事象を次の周回でもう一度扱う。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class LLMResult:
    text: str
    job_id: str
    model: str | None


def parse_json(text: str):
    """本文の中の最後の JSON オブジェクト（波括弧の対応で切り出す。文字列の中の波括弧は数えない）。無ければ None。"""
    best = None
    depth, start = 0, -1
    in_str = esc = False
    for i, ch in enumerate(text or ""):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth > 0:
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    best = json.loads(text[start:i + 1])
                except ValueError:
                    pass
    return best


class PoolLLM:
    def __init__(self, pool: WorkerPoolClient, *, role: str = "manager", poll_s: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic):
        self.pool, self.role, self.poll_s = pool, role, poll_s
        self._sleep, self._mono = sleep, monotonic

    def ask(self, prompt: str, *, sensitivity: str, deadline_s: float, project: str | None = None) -> LLMResult:
        body = {"role": self.role, "prompt": prompt, "sensitivity": sensitivity,
                "budget_min": max(1, math.ceil(deadline_s / 60))}
        if self.role == "manager":
            body["verify"] = "ax:manager-decision-v1"
        if project:
            body["project"] = project
        start = self._mono()
        if deadline_s <= 0:
            raise DecisionTimeout(f"{deadline_s}s")
        # Bound each HTTP wait by the remaining decision budget without mutating a shared client.
        pool = copy.copy(self.pool) if isinstance(self.pool, WorkerPoolClient) else self.pool
        if isinstance(pool, WorkerPoolClient):
            pool.timeout = min(pool.timeout, deadline_s)
        try:
            res = pool.submit(body)
        except Unavailable as e:
            if self._mono() - start >= deadline_s:
                raise DecisionTimeout(f"{deadline_s}s") from e
            raise LLMUnavailable("pool_unavailable") from e
        except ApiError as e:
            raise LLMUnavailable(f"submit_{e.code}") from e
        job_id = (res or {}).get("id") or (res or {}).get("job_id")
        if not job_id:
            raise LLMUnavailable(f"no_job_id:{(res or {}).get('reason', 'unknown')}")
        while True:
            remaining = deadline_s - (self._mono() - start)
            if remaining <= 0:
                self._cancel(job_id)
                raise DecisionTimeout(f"{deadline_s}s")
            if isinstance(pool, WorkerPoolClient):
                pool.timeout = min(pool.timeout, remaining)
            try:
                job = pool.job(job_id)
            except Unavailable as e:
                if self._mono() - start >= deadline_s:
                    self._cancel(job_id)
                    raise DecisionTimeout(f"{deadline_s}s") from e
                raise LLMUnavailable("pool_unavailable") from e
            except ApiError as e:
                raise LLMUnavailable(f"job_{e.code}") from e
            if self._mono() - start >= deadline_s:
                self._cancel(job_id)
                raise DecisionTimeout(f"{deadline_s}s")
            model = str((job or {}).get("model") or "").lower()
            if self.role == "manager" and model.startswith(("codex/", "claude/", "openai/", "anthropic/")):
                self._cancel(job_id)
                raise LLMUnavailable("manager_model_forbidden")
            state = (job or {}).get("state")
            if state == "ok":
                rep = job.get("report") or {}
                return LLMResult(text=rep.get("text", ""), job_id=job_id, model=job.get("model"))
            if state in TERMINAL:
                raise LLMUnavailable(f"job_{state}")
            if self._mono() - start >= deadline_s:
                self._cancel(job_id)
                raise DecisionTimeout(f"{deadline_s}s")
            self._sleep(min(self.poll_s, deadline_s - (self._mono() - start)))

    def _cancel(self, job_id: str) -> None:
        try:
            self.pool.cancel(job_id)
        except Exception:  # noqa: BLE001  取消の失敗で周回を止めない
            pass

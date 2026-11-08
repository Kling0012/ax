"""依頼 ID（idempotency_key）の照合（S-6.16 の核の部分。T-6 の一部）。

- 入口は `(destination, idempotency_key)` の組を S-17 `idempotency_retain_h` 時間だけ覚える。
  覚える物は、payload の正規形の hash、結果、最初の `principal`。
- 同じ組で hash が同じなら、処理せずに前の結果を返す（`replayed: true`）。
- 同じ組で hash が違えば、`instruction.rejected`（reason `idempotency_mismatch`）。
- 結果が確定する前の再送（ack: async で処理中）は、受付 ID と状態 `pending` を返し、二重に処理しない。
- 保持期間を過ぎた組は忘れる。

置き場は JSONL（1 行 1 件の追記）。衝突の検出（S-6.17 の保持）は後続の束（b12b3）の差し込み口
（`conflict_guard`）が担う。ここでは依頼 ID の照合だけを行う。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from axcommon import timeutil

from .registry import payload_hash

PENDING = "pending"
DONE = "done"


class Record(dict):
    """1 件の記録。destination、key、payload_hash、principal、state、status、result、ts。"""


class IdempotencyStore:
    def __init__(self, path: str | os.PathLike | None, *, retain_h: float, now_ts=timeutil.now_ts):
        self.path = Path(path) if path else None
        self.retain_h = float(retain_h)
        self._now_ts = now_ts
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], Record] = {}
        self._load()

    # ---- 保存 ----
    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue  # 壊れた行は飛ばす（落ちた時の書きかけ。fail-soft）
            if isinstance(r, dict) and "destination" in r and "key" in r:
                self._records[(r["destination"], r["key"])] = r
        self.purge_expired()

    def _append(self, r: Record) -> None:
        if self.path is None:
            return
        r = dict(r)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass  # 保存の失敗で指示を止めない（fail-soft。メモリには残る）

    def _rewrite(self) -> None:
        """期限切れを忘れた後の本文を書き直す（既定では thin しない。壊れても前の行が残る）。"""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("".join(
                json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in self._records.values()
            ), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---- 照合（S-6.16 の扱い）----
    def lookup(self, destination: str, key: str, payload: Any) -> Record | None:
        """同じ組の前の記録を返す。無ければ None。hash が違う記録は `mismatch=True` を付けて返す。"""
        with self._lock:
            r = self._records.get((destination, key))
        if r is None:
            return None
        out = Record(r)
        out["mismatch"] = r.get("payload_hash") != payload_hash(payload)
        return out

    def begin(self, destination: str, key: str, payload: Any, principal: str) -> Record:
        """受理を記録する（pending）。既にある組は上書きしない（最初の principal と結果を保つ。S-6.16）。"""
        self.purge_expired()
        with self._lock:
            existing = self._records.get((destination, key))
            if existing is not None:
                return Record(existing)
            r = Record({"destination": destination, "key": key, "payload_hash": payload_hash(payload),
                        "principal": principal, "state": PENDING, "status": None, "result": None, "ts": self._now_ts()})
            self._records[(destination, key)] = r
        self._append(r)
        return r

    def finish(self, destination: str, key: str, *, status: int, result: Any) -> None:
        with self._lock:
            r = self._records.get((destination, key))
            if r is None:
                return
            r["state"] = DONE
            r["status"] = int(status)
            r["result"] = result
            r["done_ts"] = self._now_ts()
        self._append(r)

    def purge_expired(self) -> None:
        """保持期間を過ぎた組を忘れる（S-6.16）。"""
        if not self._records:
            return
        try:
            cutoff = timeutil.parse_ts(self._now_ts())
        except (ValueError, TypeError):
            return
        from datetime import timedelta

        limit = cutoff - timedelta(hours=self.retain_h)
        with self._lock:
            before = len(self._records)
            self._records = {k: r for k, r in self._records.items()
                             if _parse_or_none(r.get("ts")) is None or _parse_or_none(r["ts"]) >= limit}
            dropped = before - len(self._records)
        if dropped:
            self._rewrite()

    def __len__(self) -> int:
        return len(self._records)


def _parse_or_none(ts: Any):
    if not isinstance(ts, str):
        return None
    try:
        return timeutil.parse_ts(ts)
    except ValueError:
        return None

"""予備役の頻度（S-11.4、D-10.2）。回数は `backup.activated` の数で数える。

正本は時系列記録の `backup.activated`。時系列記録に届かない間は、自分の保存先（activations.jsonl）の数を使う。
両方が取れたときは大きい方（同じ起動を数えているので、記録が欠けた側を補う）。
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from axcommon import timeutil
from axcommon.client import ApiError, TimelineClient, Unavailable

PAGE = 1000
MAX_PAGES = 20


class ActivationLog:
    def __init__(self, directory: str | os.PathLike):
        self.path = Path(directory) / "activations.jsonl"

    def add(self, ts: str, check: str, target: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "check": check, "target": target}, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def count_since(self, since_ts: str) -> int:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        n = 0
        for line in lines:
            try:
                ts = json.loads(line).get("ts")
            except (ValueError, AttributeError):
                continue
            if isinstance(ts, str) and ts >= since_ts:
                n += 1
        return n


def _events(resp) -> list[dict]:
    if isinstance(resp, list):
        return [e for e in resp if isinstance(e, dict)]
    if isinstance(resp, dict):
        for k in ("events", "items"):
            if isinstance(resp.get(k), list):
                return [e for e in resp[k] if isinstance(e, dict)]
    return []


def count_from_timeline(timeline: TimelineClient, since_ts: str) -> int | None:
    """時系列記録の backup.activated のうち since_ts 以後の数。届かなければ None。"""
    after, total = 0, 0
    try:
        for _ in range(MAX_PAGES):
            evs = _events(timeline.get_events(after=after, kind="backup.activated", limit=PAGE))
            if not evs:
                break
            total += sum(1 for e in evs if isinstance(e.get("ts"), str) and e["ts"] >= since_ts)
            seqs = [e.get("seq") for e in evs if isinstance(e.get("seq"), int)]
            if not seqs or len(evs) < PAGE:
                break
            after = max(seqs)
    except (Unavailable, ApiError):
        return None
    return total


def window_start(now, days: float) -> str:
    return timeutil.format_ts(now - timedelta(days=days))


def count_activations(*, timeline: TimelineClient | None, log: ActivationLog, now, days: float) -> int:
    since = window_start(now, days)
    local = log.count_since(since)
    remote = count_from_timeline(timeline, since) if timeline is not None else None
    return max(local, remote or 0)

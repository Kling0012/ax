"""予定の時刻の判定（S-7.5「予定の時刻に起動できなかったら missed を書き、追いかけない」）。サブループの scheduler と timer-runner が共有する。

decide(kind, spec, next_due, now) -> Decision
  next_due : 次の予定の時刻（None なら now を最初の予定にする）
  戻り: missed（間に合わなかった予定の時刻の並び。追いかけない）、run（今走らせる予定の時刻。無ければ None）、next_due（次の予定）。
- interval: 予定は next_due + k * every_s。いま過ぎた予定のうち最新の 1 つだけが対象。最新でも許容（grace = max(10 秒, every_s の半分)）を超えて遅れたら missed。
  最新より古い予定は、すべて missed。
- cron: 予定は cron の次の時刻。120 秒を超えて遅れたら missed。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from axcommon import timeutil

CRON_GRACE_S = 120.0


@dataclass
class Decision:
    missed: list[str] = field(default_factory=list)
    run: str | None = None
    next_due: str | None = None


def decide(kind: str, spec: dict, next_due: str | None, now: datetime) -> Decision:
    d = Decision()
    if kind == "interval":
        every = int(spec["every_s"])
        nd = timeutil.parse_ts(next_due) if next_due else now
        grace = timedelta(seconds=max(10.0, every * 0.5))
        while nd <= now:
            after = nd + timedelta(seconds=every)
            if after <= now or now - nd > grace:
                d.missed.append(timeutil.format_ts(nd))
            else:
                d.run = timeutil.format_ts(nd)
                nd = after
                break
            nd = after
        d.next_due = timeutil.format_ts(nd)
        return d
    if kind == "cron":
        from .cron import next_after

        if not next_due:
            d.next_due = timeutil.format_ts(next_after(spec["cron"], now))
            return d
        nd = timeutil.parse_ts(next_due)
        if nd > now:
            d.next_due = next_due
            return d
        if now - nd > timedelta(seconds=CRON_GRACE_S):
            d.missed.append(timeutil.format_ts(nd))
        else:
            d.run = timeutil.format_ts(nd)
        d.next_due = timeutil.format_ts(next_after(spec["cron"], now))
        return d
    d.next_due = next_due
    return d

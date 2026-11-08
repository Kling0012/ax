"""最小の cron（5 項目: 分 時 日 月 曜日。`*`、`a,b`、`a-b`、`*/n`、`a-b/n`）。時刻は UTC。

timezone の扱いは文書にない（OPEN.md）。曜日は 0 または 7 が日曜。日と曜日の両方を指定したときは両方を満たす日だけ（標準の cron の「どちらか」とは違う）。
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = int(x), int(y)
        else:
            a = b = int(part)
        if a < lo or b > hi or step < 1 or a > b:
            raise ValueError(f"cron の項目が範囲外: {spec}")
        out.update(range(a, b + 1, step))
    return out


def parse(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    f = expr.split()
    if len(f) != 5:
        raise ValueError("cron は 5 項目")
    dow = {0 if d == 7 else d for d in _field(f[4], 0, 7)}
    return _field(f[0], 0, 59), _field(f[1], 0, 23), _field(f[2], 1, 31), _field(f[3], 1, 12), dow


def next_after(expr: str, after: datetime) -> datetime:
    mi, ho, dom, mon, dow = parse(expr)
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 400):
        if t.minute in mi and t.hour in ho and t.day in dom and t.month in mon and ((t.weekday() + 1) % 7) in dow:
            return t
        t += timedelta(minutes=1)
    raise ValueError("400 日以内に次の時刻が無い")

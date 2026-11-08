"""時刻（S-2.1: UTC の RFC 3339、ミリ秒まで）。試験で時計を差し替えられる。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$")
_clock: Callable[[], datetime] | None = None


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """時計を差し替える。None で戻す。fn は tz つきの datetime を返す。"""
    global _clock
    _clock = fn


def now() -> datetime:
    return _clock() if _clock else datetime.now(timezone.utc)


def format_ts(dt: datetime) -> str:
    """UTC の RFC 3339（ミリ秒。例 2026-11-05T12:00:00.000Z）。tz なしの datetime は UTC とみなす。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def now_ts() -> str:
    return format_ts(now())


def parse_ts(text: str) -> datetime:
    """RFC 3339 を tz つきの datetime にする。+09:00 などの時差も受ける。"""
    m = _TS_RE.match(text)
    if not m:
        raise ValueError(f"RFC 3339 でない: {text!r}")
    y, mo, d, h, mi, s, frac, tz = m.groups()
    micro = int((frac or "0").ljust(6, "0"))
    if tz == "Z":
        tzinfo = timezone.utc
    else:
        sign = 1 if tz[0] == "+" else -1
        tzinfo = timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6])))
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro, tzinfo)


def add_seconds(ts: str, seconds: float) -> str:
    return format_ts(parse_ts(ts) + timedelta(seconds=seconds))

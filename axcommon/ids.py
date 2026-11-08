"""ULID（S-2.1）。標準ライブラリだけの実装。python-ulid と同じ形式（Crockford の base32、48 ビットの時刻 + 80 ビットの乱数）。

同じミリ秒の中では乱数部を 1 ずつ増やして単調にする（時刻順に並ぶ。D-1）。
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_ALPHABET)}
ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_lock = threading.Lock()
_last_ms = -1
_last_rand = 0
_last_explicit = (-1, 0)


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_ulid(now_ms: int | None = None, randbytes: Callable[[int], bytes] = os.urandom) -> str:
    """新しい ULID を返す。now_ms は試験用（既定は現在時刻）。

    現在時刻を使うときは、時計が戻っても単調に増える（直前より小さくならない）。
    now_ms を渡したときは、その時刻をそのまま使い（直前の ULID に引きずられない）、同じ値の間だけ乱数部を増やして単調にする。
    """
    global _last_ms, _last_rand, _last_explicit
    explicit = now_ms is not None
    ms = int(now_ms) if explicit else int(time.time() * 1000)
    if ms < 0 or ms >= 1 << 48:
        raise ValueError("ULID の時刻が範囲外")
    with _lock:
        last_ms, last_rand = _last_explicit if explicit else (_last_ms, _last_rand)
        if not explicit and ms < last_ms:
            ms = last_ms  # 時計が戻っても直前より小さくしない
        if ms == last_ms:
            rand = last_rand + 1
            if rand >= 1 << 80:  # 同じミリ秒に 2^80 個は来ない。来たら次のミリ秒へ
                ms += 1
                rand = int.from_bytes(randbytes(10), "big")
        else:
            rand = int.from_bytes(randbytes(10), "big")
        if explicit:
            _last_explicit = (ms, rand)
        else:
            _last_ms, _last_rand = ms, rand
    return _encode(ms, 10) + _encode(rand, 16)


def is_ulid(value: object) -> bool:
    return isinstance(value, str) and ULID_RE.match(value) is not None


def ulid_time_ms(value: str) -> int:
    """ULID の時刻部（エポックからのミリ秒）。"""
    if not is_ulid(value):
        raise ValueError(f"ULID でない: {value!r}")
    n = 0
    for c in value[:10]:
        n = (n << 5) | _DECODE[c]
    return n

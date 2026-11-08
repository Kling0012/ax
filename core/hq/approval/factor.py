"""RFC 6238 verification. Only this function changes for another factor."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct


def verify_code(seed: str, code: str, now: float, last_step: int) -> int | None:
    """Return a fresh matching step; never return a step twice or accept integers."""
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        return None
    key = base64.b32decode(seed, casefold=False)
    current = int(now // 30)
    # Check the entire window, including old steps. A colliding reused code must
    # not become usable by advancing to a different step with the same digits.
    matches = []
    for step in range(max(0, current - 1), current + 2):
        mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
        offset = mac[-1] & 15
        value = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7fffffff) % 1_000_000
        if hmac.compare_digest(f"{value:06d}", code):
            matches.append(step)
    if any(step <= last_step for step in matches):
        return None
    return max(matches) if matches else None

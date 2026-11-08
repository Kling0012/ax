from __future__ import annotations

from collections import defaultdict, deque
import math
import time

from .identity import Denied


class Limited(Denied):
    def __init__(self, retry_after):
        super().__init__("rate_limited", 429)
        self.retry_after = str(max(1, math.ceil(retry_after)))


class Limits:
    def __init__(self, settings, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.calls = defaultdict(deque)
        self.inflight = defaultdict(int)
        self.enroll = {}

    def enter(self, principal, write=False):
        now = self.clock()
        period, maximum = (60, self.settings.gate_write_per_min) if write else (1, self.settings.gate_read_per_s)
        calls = self.calls[(principal, write)]
        while calls and calls[0] <= now - period:
            calls.popleft()
        if len(calls) >= maximum:
            raise Limited(calls[0] + period - now)
        if self.inflight[principal] >= self.settings.gate_inflight_max:
            raise Limited(1)
        calls.append(now)
        self.inflight[principal] += 1

    def leave(self, principal):
        self.inflight[principal] -= 1

    def enroll_check(self, device):
        failures, until = self.enroll.get(device, (0, 0))
        if until > self.clock():
            raise Denied("locked", 503)
        if until:
            self.enroll.pop(device, None)

    def enroll_result(self, device, success):
        if success:
            self.enroll.pop(device, None)
            return
        count = self.enroll.get(device, (0, 0))[0] + 1
        until = self.clock() + self.settings.gate_enroll_lock_min * 60 if count >= self.settings.gate_enroll_fail_max else 0
        self.enroll[device] = count, until

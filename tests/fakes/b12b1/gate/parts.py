from __future__ import annotations

import asyncio
from dataclasses import dataclass

from core.gate.contracts import Reply


class FakeRegistry:
    def __init__(self, entries=None):
        self.entries = entries or {}
        self.reloads = 0

    def get(self, name):
        return self.entries.get(name)

    def maybe_reload(self):
        self.reloads += 1
        return False


class FakeRelay:
    def __init__(self, catalog, replies=None):
        self.catalog, self.replies, self.calls = catalog, replies or {}, []
        self.wait = None

    async def __call__(self, request, timeout):
        self.calls.append(request)
        if self.wait:
            await asyncio.sleep(self.wait)
        if request.path == "/v1/read/destinations":
            return Reply(200, self.catalog)
        reply = self.replies.get((request.method, request.path))
        if callable(reply):
            return reply(request)
        return reply or Reply(200, {"ok": True})

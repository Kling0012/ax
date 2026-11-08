"""偽の台帳・入口。ネットワークを使わない b10b API 試験用。"""
from __future__ import annotations

from datetime import datetime, timezone

from axcommon.client import ApiError, Unavailable


class FakeLedger:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []
        self.down = False

    def get(self, path, *, params=None):
        self.calls.append((path, dict(params or {})))
        if self.down:
            raise Unavailable("fake_down")
        value = self.values.get((path, tuple(sorted((params or {}).items()))), self.values.get(path))
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ApiError(404, "not_found")
        return value


class FakeIntake:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.down = False

    def submit(self, destination, payload, *, idempotency_key, confirm_irreversible=False, session=None):
        self.calls.append({"destination": destination, "payload": payload, "idempotency_key": idempotency_key})
        if self.down:
            raise ConnectionError("fake_down")
        return self.responses.get(destination, (200, {"ok": True, "result": {"todo_id": "todo-1"}}))


class FakeDefaults:
    def __init__(self, values=None):
        self.values = {"u_hibi_interval_s": 60, "hibi_future_skew_min": 5, "hibi_segments_max": 288}
        self.values.update(values or {})

    def get(self, name):
        return self.values[name]


def fixed_now():
    return datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)

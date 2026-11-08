"""In-memory seat backend; no sockets, services or subprocesses."""
from __future__ import annotations

from datetime import datetime, timezone
import threading

from ax_gate import GateError


class FakeSeatBackend:
    def __init__(self, rows):
        self.rows = {row["seat_id"]: dict(row) for row in rows}
        self.sessions = {}
        self.closed = []
        self.lock = threading.RLock()
        self.sequence = 0
        self.failure = None
        self.ssh_locked = False

    def _row(self, seat_id):
        if self.failure:
            raise self.failure
        row = self.rows.get(seat_id)
        if row is None:
            raise GateError("seat_unknown", 7)
        if self.ssh_locked:
            raise GateError("locked", 7)
        if row["state"] != "active":
            raise GateError("seat_" + row["state"], 7)
        if datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise GateError("seat_expired", 7)
        return row

    def open(self, seat_id, fingerprint, maximum):
        with self.lock:
            row = self._row(seat_id)
            if fingerprint != row["key_fp"]:
                raise GateError("seat_unknown", 7)
            if sum(value == seat_id for value in self.sessions.values()) >= maximum:
                raise GateError("locked", 7)
            self.sequence += 1
            session = "fake-session-" + str(self.sequence)
            self.sessions[session] = seat_id
            return {"session": session, "key_fp": row["key_fp"], "tier": row["tier"]}

    def check(self, session):
        with self.lock:
            return self._row(self.sessions[session])

    def close(self, session, counts):
        with self.lock:
            self.closed.append((session, dict(counts)))
            self.sessions.pop(session, None)


class FakeDispatch:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.hook = None

    def __call__(self, words, stdin, stdout, stderr, session):
        self.calls.append((words, stdin.read(), session))
        if self.hook:
            self.hook()
        if self.failure:
            raise self.failure
        stdout.write("{}\n")
        return 0

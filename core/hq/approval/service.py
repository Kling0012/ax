"""Stateful factor checks, enrollment and host-operation warnings."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Callable
import secrets
import logging
import time
from urllib.parse import urlencode

from .factor import verify_code
from .private import PrivateStore
from .security import Identity, VerifiedAction

log = logging.getLogger("hq.approval.factor")


class ApprovalFailure(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


@dataclass(frozen=True)
class FactorAdapters:
    """Durable, idempotent event and notification adapters for factor changes."""
    emit: Callable[[dict], None]
    notify: Callable[[dict], None]

    def __post_init__(self):
        if not callable(self.emit) or not callable(self.notify):
            raise ValueError("factor_adapters_invalid")


class FactorService:
    def __init__(self, store: PrivateStore, *, checker=verify_code, clock=time.time,
                 fail_max=5, lock_min=15, audit_window_s=300, emit=None, notify=None, qr_renderer=None):
        if not 3 <= fail_max <= 10 or not 5 <= lock_min <= 120:
            raise ValueError("approval_factor_limits")
        self.store, self.checker, self.clock = store, checker, clock
        self.fail_max, self.lock_min, self.audit_window_s = fail_max, lock_min, audit_window_s
        self.emit, self.notify, self.qr_renderer = emit, notify, qr_renderer

    def _queue(self, state, kind, **data):
        if self.emit is None or self.notify is None:
            raise NotImplementedError("HQ factor event/notification adapters missing")
        state.setdefault("outbox", []).append({"id": secrets.token_hex(16), "kind": kind,
                                               "at": self.clock(), "data": data})

    def _flush(self, state):
        pending = state.get("outbox", [])
        while pending:
            item = pending[0]
            try:
                # Adapters deduplicate by id; neither receives seed or code.
                self.emit(dict(item))
                self.notify(dict(item))
            except Exception:
                log.error("factor delivery pending")
                break
            pending.pop(0)
        self.store.write(state)

    def _check(self, state, code):
        now = self.clock()
        if state.get("locked_until", 0) > now:
            raise ApprovalFailure(403, "approval_locked")
        if not state.get("seed"):
            raise ApprovalFailure(403, "factor_not_enrolled")
        step = self.checker(state["seed"], code, now, state.get("last_step", -1))
        if step is None:
            failures = state.get("failures", 0) + 1
            # Match S-6.20's auth.failed aggregation. The transition to a lock
            # has its own reason and must still notify even inside that window.
            if state.get("last_auth_notice") is None or now - state["last_auth_notice"] >= self.audit_window_s:
                self._queue(state, "auth.failed", reason="approval_code_invalid")
                state["last_auth_notice"] = now
            state["failures"] = failures
            if failures >= self.fail_max:
                self._queue(state, "auth.failed", reason="approval_locked")
                state.update(locked_until=now + self.lock_min * 60, failures=0)
            self.store.write(state)
            self._flush(state)
            raise ApprovalFailure(403, "approval_code_invalid")
        if not isinstance(step, int) or step <= state.get("last_step", -1):
            raise ApprovalFailure(403, "approval_code_invalid")
        state.update(last_step=step, failures=0, locked_until=0, last_auth_notice=None)
        self.store.write(state)

    def verify(self, identity: Identity, code: str) -> VerifiedAction:
        if not isinstance(identity, Identity) or not identity.allowed or not identity.subject or not identity.session or not identity.direct_tailnet:
            raise ApprovalFailure(403, "unauthorized")
        with self.store.locked():
            state = self.store.read()
            if self.emit is None or self.notify is None:
                raise ApprovalFailure(503, "factor_delivery_unavailable")
            if state.get("outbox"):
                self._flush(state)
                if state.get("outbox"):
                    raise ApprovalFailure(503, "factor_delivery_pending")
            self._check(state, code)
        return VerifiedAction(identity, True)

    def enroll(self, identity: Identity, code: str | None = None) -> dict:
        if not isinstance(identity, Identity) or not identity.allowed or not identity.subject or not identity.session or not identity.direct_tailnet:
            raise ApprovalFailure(403, "unauthorized")
        if self.qr_renderer is None:
            raise NotImplementedError("F-13.5: QR renderer binding missing")
        if self.emit is None or self.notify is None:
            raise NotImplementedError("HQ factor event/notification adapters missing")
        with self.store.locked():
            old = self.store.read()
            if old.get("seed"):
                if old.get("outbox"):
                    self._flush(old)
                    if old.get("outbox"):
                        raise ApprovalFailure(503, "factor_delivery_pending")
                self._check(old, code)
            seed = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
            # No email or host name in the QR account label.
            uri = "otpauth://totp/AX:approval?" + urlencode({"secret": seed, "issuer": "AX",
                    "algorithm": "SHA1", "digits": "6", "period": "30"})
            svg = self.qr_renderer(uri)
            if not isinstance(svg, str) or not svg:
                raise ApprovalFailure(503, "qr_unavailable")
            state = {"seed": seed, "last_step": -1, "failures": 0, "locked_until": 0,
                     "enrolled_at": self.clock(), "outbox": old.get("outbox", [])}
            self._queue(state, "approval.factor.enrolled")
            self.store.write(state)
            self._flush(state)
            # Only this call returns the QR. GETs and repeated page POSTs cannot
            # retrieve it; the page token is consumed before enrollment.
            return {"enrolled": True, "qr_svg": svg, "display_once": True}

    def observe_host_event(self, *, event_id: str, kind: str, at: float):
        """Trusted timeline adapter only. No HTTP endpoint can invoke this.

        host/ax-hq-enroll and seat ownership remain outside HQ. Their event
        producers own reset/host-added timeline entries and notifications.
        """
        if kind not in {"approval.factor.reset", "seat.host_added"}:
            raise ValueError("host_event_kind")
        if not event_id or not isinstance(at, (int, float)) or at > self.clock() + 30:
            raise ValueError("host_event")
        with self.store.locked():
            alerts = self.store.read("approval-alerts")
            previous = alerts.get(kind, {})
            if at >= previous.get("at", 0):
                alerts[kind] = {"event_id": event_id, "at": at}
                self.store.write(alerts, "approval-alerts")

    def status(self) -> dict:
        with self.store.locked():
            state = self.store.read()
            if state.get("outbox"):
                self._flush(state)
            alerts = self.store.read("approval-alerts")
            now = self.clock()
            return {"enrolled": bool(state.get("seed")), "locked_until": state.get("locked_until", 0),
                    "notification_pending": bool(state.get("outbox")),
                    "warnings": [{"kind": k, "at": v["at"], "until": v["at"] + 86400}
                                 for k, v in alerts.items() if now < v["at"] + 86400]}

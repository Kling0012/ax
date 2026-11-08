"""S-6.20 audit integration; read summaries remain owned by b12b3."""
from __future__ import annotations

import contextvars
import re
import threading
from urllib.parse import urlsplit

from axcommon import timeutil
from axcommon.redact import Redactor

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_IP = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F]*:[0-9a-fA-F:]*:[0-9a-fA-F:]+")


def safe_label(value, redactor):
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or _IP.search(value) or redactor.scan(value):
        return None
    return value


def _without_addresses(value):
    if isinstance(value, str):
        return _IP.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {key: _without_addresses(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_without_addresses(child) for child in value]
    return value


class ApprovalAudit:
    def __init__(self, *, sink=None, access=None, redactor=None, caller_reader=None, audit_window_s=300):
        self.sink, self.access = sink, access
        self.redactor = redactor or Redactor()
        self.caller_reader = caller_reader
        self.audit_window_s = audit_window_s
        self._auth_windows = {}
        self._auth_lock = threading.Lock()

    def caller(self, ident, session=None) -> dict:
        # This callback receives verified connection context, never request JSON.
        context = self.caller_reader(ident) if self.caller_reader else {}
        context = context or {}
        declared = context.get("declared") or {}
        return {"via": ident.via, "device": safe_label(context.get("device"), self.redactor),
                "key_fp_short": safe_label((context.get("key_fp") or "")[:12], self.redactor),
                "tier": ident.max_tier,
                "declared": {k: safe_label(declared.get(k), self.redactor) for k in ("agent", "model")}}

    def emit(self, kind: str, subject: str, ident, *, session=None, project=None, data=None):
        if self.sink is None:
            return
        safe_data = dict(data or {})
        safe_data["caller"] = self.caller(ident)
        safe_data, _ = self.redactor.redact_obj(_without_addresses(safe_data))
        # No payload, reason, HQ identity, peer address, code, key, or token is admitted.
        context = self.caller_reader(ident) if self.caller_reader else {}
        source = {"principal": safe_label(ident.principal, self.redactor) or "unknown",
                  "via": ident.via, "session": safe_label((context or {}).get("session"), self.redactor) or "unknown"}
        try:
            self.sink.emit(kind, safe_label(subject, self.redactor) or "intake", safe_data,
                           source_extra=source, project=safe_label(project, self.redactor))
        except Exception:
            # EventSink persists to its outbox. A broken sink must not disable access.log.
            pass

    def request(self, ident, method: str, path: str, status: int):
        if self.access:
            # The query can contain secrets; accept only known endpoint shapes.
            path = urlsplit(path).path
            if not re.fullmatch(r"/v1/(?:instructions|approvals(?:/[A-Za-z0-9_-]+(?:/grant)?)?|read/[a-z_-]+)", path):
                path = "/unrecognized"
            try:
                self.access.write(ts=timeutil.now_ts(), via=ident.via,
                                  principal=safe_label(ident.principal, self.redactor),
                                  method=method if method in {"GET", "POST"} else "OTHER", path=path, status=status)
            except OSError:
                pass

    def auth_failed(self, ident, reason):
        now = timeutil.now().timestamp()
        caller = self.caller(ident)
        key = (caller["device"] or ident.principal, reason)
        with self._auth_lock:
            self._auth_windows = {k: v for k, v in self._auth_windows.items() if now - v < self.audit_window_s}
            if key in self._auth_windows:
                return
            self._auth_windows[key] = now
        self.emit("auth.failed", "intake", ident,
                  data={"via": ident.via, "reason": reason, "device": caller["device"], "count": 1})

    def denied(self, ident, destination, reason, *, session=None):
        data = {"principal": ident.principal, "destination": destination, "reason": reason}
        self.emit("commander.denied", "intake", ident, session=session, data=data)
        self.emit("instruction.rejected", "intake", ident, session=session,
                  data={"destination": destination, "reason": reason})


REQUEST_IDENT = contextvars.ContextVar("approval_request_ident", default=None)


class VerifiedIntakeSink:
    """Enrich B5 instruction events using the verified connection context."""
    def __init__(self, delegate, audit):
        self.delegate, self.audit = delegate, audit

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def emit(self, kind, subject, data=None, **kwargs):
        ident = REQUEST_IDENT.get()
        if ident is not None:
            context = self.audit.caller_reader(ident) if self.audit.caller_reader else {}
            session = (context or {}).get("session") or (kwargs.get("source_extra") or {}).get("session")
            kwargs["source_extra"] = {"principal": safe_label(ident.principal, self.audit.redactor) or "unknown",
                                      "via": ident.via,
                                      "session": safe_label(session, self.audit.redactor) or "unknown"}
            data = {**(data or {}), "caller": self.audit.caller(ident)}
            data, _ = self.audit.redactor.redact_obj(_without_addresses(data))
        return self.delegate.emit(kind, subject, data, **kwargs)


class AuditContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = REQUEST_IDENT.set(scope.get("ax_ident"))
        try:
            await self.app(scope, receive, send)
        finally:
            REQUEST_IDENT.reset(token)

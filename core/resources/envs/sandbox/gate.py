"""Gate admission service. Storage and provider checks are supplied by the host.

The host must share one slot store with route J and wire the public HTTP API to
this service; this module does not open a listener or connect to providers.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .free_guard import check_free
from .probe_common import ProviderState, gate_ready

CAPABILITIES = frozenset({"linux-sandbox", "web-screenshot", "web-screenshot-full", "web-gui", "sandbox-desktop", "scheduled-publisher-cli", "docker"})


class GateDenied(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class IssuedToken:
    token_id: str
    token: str
    expires_at: float
    gate_path: str

    def __repr__(self):
        return f"IssuedToken(token_id={self.token_id!r}, token=<redacted>, expires_at={self.expires_at!r}, gate_path={self.gate_path!r})"


class GateService:
    """Single-process admission component; callers must share its slot lock.

    Probe and measurement callbacks are read-only and must raise on failure.
    This is not a substitute for durable token storage or HTTP authorization.
    """
    def __init__(self, *, auth_digest: str, state: ProviderState, capabilities: set[str],
                 read_worker_key: Callable, measure: Callable, reserve: Callable,
                 release: Callable, clock: Callable = time.time, max_age_s: float = 300,
                 max_budget_min: int = 120, token_max_min: int = 120):
        if not re.fullmatch(r"[0-9a-f]{64}", auth_digest):
            raise ValueError("invalid_auth_digest")
        self._auth_digest = auth_digest
        self.state, self.capabilities = state, frozenset(capabilities)
        self._read_worker_key, self._measure = read_worker_key, measure
        self._reserve, self._release = reserve, release
        self._clock, self._max_age_s = clock, max_age_s
        self._max_budget_min, self._token_max_min = max_budget_min, token_max_min
        self._lock = threading.RLock()
        self._tokens = {}

    def issue(self, authorization: str, request: dict) -> IssuedToken:
        if not isinstance(authorization, str) or not re.fullmatch(r"Bearer [0-9a-f]{64}", authorization):
            raise GateDenied("unauthenticated")
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        if not hmac.compare_digest(digest, self._auth_digest):
            raise GateDenied("unauthenticated")
        if not isinstance(request, dict):
            raise GateDenied("bad_request")
        job = request.get("job_id")
        budget = request.get("budget_min")
        tools = request.get("tools")
        if (not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job)
                or type(budget) is not int or not 1 <= budget <= self._max_budget_min
                or not isinstance(tools, list) or any(not isinstance(t, str) for t in tools)
                or type(request.get("needs_vision", False)) is not bool):
            raise GateDenied("bad_request")
        if request.get("role") not in {"research", "screen", "assist"}:
            raise GateDenied("role_denied")
        if request.get("sensitivity") != "public":
            raise GateDenied("sensitivity_denied")
        if not tools:
            raise GateDenied("tools_missing")
        if not set(tools) <= CAPABILITIES:
            raise GateDenied("tools_unknown")
        with self._lock:
            self._expire()
            if self.state.gate_state != "available" and not (self.state.gate_state == "trial" and self.state.state == "available" and request.get("trial_for") == self.state.env_id):
                raise GateDenied("gate_state")
            try:
                key = self._read_worker_key()
                if not isinstance(key, (str, bytes)) or not key:
                    raise ValueError()
            except Exception:
                self.state.worker_key_unavailable()
                self.revoke_all()
                raise GateDenied("worker_key_missing") from None
            if not gate_ready(self.state, self._clock(), self._max_age_s):
                raise GateDenied("gate_not_ready")
            if not set(tools) <= self.capabilities:
                raise GateDenied("tools_not_available")
            if "sandbox-desktop" in tools and not request.get("needs_vision", False):
                raise GateDenied("vision_required")
            if any(row["job"] == job for row in self._tokens.values()):
                raise GateDenied("token_exists")
            try:
                measurement = self._measure()
            except Exception:
                raise GateDenied("free_unverified") from None
            reason = check_free(measurement, budget)
            if reason:
                raise GateDenied(reason)
            if not self._reserve(self.state.env_id, job):
                raise GateDenied("slot_busy")
            try:
                token_id = secrets.token_hex(16)
                token = "axg-" + secrets.token_hex(32)
                expires = self._clock() + min(budget + 5, self._token_max_min) * 60
                self._tokens[token_id] = {"digest": hashlib.sha256(token.encode()).digest(), "job": job, "expires": expires}
                return IssuedToken(token_id, token, expires, f"/mcp/{self.state.env_id}")
            except BaseException:
                self._release(self.state.env_id, job)
                raise

    def validate(self, token: str) -> bool:
        if not isinstance(token, str) or not re.fullmatch(r"axg-[0-9a-f]{64}", token):
            return False
        with self._lock:
            self._expire()
            digest = hashlib.sha256(token.encode()).digest()
            return self.state.gate_state in {"available", "trial"} and any(hmac.compare_digest(digest, row["digest"]) for row in self._tokens.values())

    def revoke(self, token_id: str) -> bool:
        with self._lock:
            row = self._tokens.pop(token_id, None)
            if row is None:
                return False
            self._release(self.state.env_id, row["job"])
            return True

    def revoke_all(self):
        with self._lock:
            for token_id in list(self._tokens):
                self.revoke(token_id)

    def _expire(self):
        if self.state.gate_state == "excluded":
            self.revoke_all()
            return
        now = self._clock()
        for token_id in [k for k, v in self._tokens.items() if v["expires"] <= now]:
            self.revoke(token_id)

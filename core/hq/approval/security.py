"""Trusted HQ identity, per-screen CSRF and browser request checks."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import inspect
import secrets
import threading
import time


class BoundaryError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass(frozen=True)
class Identity:
    """Returned by a trusted auth adapter, never parsed from HTTP headers here.

    subject/session are opaque and stay in memory; no login is sent to intake.
    The adapter must apply S-22.7 (including trusted serve source classification).
    """
    subject: str
    session: str
    direct_tailnet: bool
    allowed: bool = True


@dataclass(frozen=True)
class VerifiedAction:
    identity: Identity
    second_factor: bool


class ScreenTokens:
    def __init__(self, *, clock=time.monotonic, ttl=600, limit=512):
        self.clock, self.ttl, self.limit = clock, ttl, limit
        self._tokens = OrderedDict()
        self._mutex = threading.Lock()

    def issue(self, identity: Identity, screen: str, *, digest: str | None = None) -> str:
        with self._mutex:
            self._prune()
            while len(self._tokens) >= self.limit:
                self._tokens.popitem(last=False)
            token = secrets.token_urlsafe(32)
            self._tokens[token] = (identity, screen, digest, self.clock() + self.ttl)
            return token

    def _prune(self):
        now = self.clock()
        for token in list(self._tokens):
            if self._tokens[token][3] <= now:
                self._tokens.pop(token)

    def consume(self, token, identity: Identity, screen: str) -> str | None:
        with self._mutex:
            self._prune()
            saved = self._tokens.get(token) if isinstance(token, str) else None
            if not saved or saved[:2] != (identity, screen):
                raise BoundaryError(403, 'csrf_invalid')
            self._tokens.pop(token)
            return saved[2]


class BrowserBoundary:
    def __init__(self, allowed_hosts=(), allowed_origins=()):
        self.hosts, self.origins = frozenset(allowed_hosts), frozenset(allowed_origins)

    async def authorize(self, request) -> Identity:
        # Never infer public/direct routing from Host. Only the authenticated
        # source classifier may supply this context (S-22.7).
        auth = getattr(request.app.state, 'hq_approval_identity', None)
        if auth is None:
            raise NotImplementedError('S-22.7 authenticated HQ identity binding missing')
        identity = auth(request)
        if inspect.isawaitable(identity):
            identity = await identity
        if not isinstance(identity, Identity):
            raise BoundaryError(403, 'unauthorized')
        if not identity.direct_tailnet:
            raise BoundaryError(404, 'not_found')
        if not identity.allowed or not identity.subject or not identity.session:
            raise BoundaryError(403, 'unauthorized')
        if not self.hosts or not self.origins:
            raise NotImplementedError('HQ Host/Origin configuration missing')
        # Multiple header values must not be collapsed into a permissive one.
        if len(request.headers.getlist('host')) != 1 or request.headers['host'] not in self.hosts:
            raise BoundaryError(421, 'host_not_allowed')
        sites = request.headers.getlist('sec-fetch-site')
        if len(sites) > 1 or (sites and sites[0] not in {'same-origin', 'none'}):
            raise BoundaryError(403, 'cross_site')
        origins = request.headers.getlist('origin')
        if len(origins) > 1 or (origins and origins[0] not in self.origins):
            raise BoundaryError(403, 'origin_not_allowed')
        if request.method == 'POST' and not origins:
            raise BoundaryError(403, 'origin_required')
        return identity

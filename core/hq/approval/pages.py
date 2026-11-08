"""Per-page, single-use CSRF state bound to authenticated sessions and contents."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hmac
import secrets
import threading
import time

from .service import ApprovalFailure, Identity


@dataclass
class Page:
    owner: tuple[str, str]
    purpose: str
    token: str
    expires: float
    saved: dict


class Pages:
    def __init__(self, *, clock=time.time, ttl_s=900, limit=256):
        self.clock, self.ttl_s, self.limit = clock, ttl_s, limit
        self._items = {}
        self._lock = threading.Lock()

    def issue(self, identity: Identity, purpose: str, saved: dict) -> dict:
        with self._lock:
            self._items = {k: p for k, p in self._items.items() if p.expires > self.clock()}
            if len(self._items) >= self.limit:
                raise ApprovalFailure(429, "page_limit")
            page_id, token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
            self._items[page_id] = Page((identity.subject, identity.session), purpose, token,
                                      self.clock() + self.ttl_s, deepcopy(saved))
            return {"page_id": page_id, "csrf_token": token}

    def consume(self, identity: Identity, purpose: str, body: dict) -> dict:
        page_id, token = body.get("page_id"), body.get("csrf_token")
        if not isinstance(page_id, str) or not isinstance(token, str):
            raise ApprovalFailure(403, "csrf_invalid")
        with self._lock:
            page = self._items.get(page_id)
            if (page is None or page.expires <= self.clock() or page.purpose != purpose
                    or page.owner != (identity.subject, identity.session)
                    or not hmac.compare_digest(token, page.token)):
                raise ApprovalFailure(403, "csrf_invalid")
            self._items.pop(page_id)
            return deepcopy(page.saved)

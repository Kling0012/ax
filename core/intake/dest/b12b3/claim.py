"""`claim.op` destination: explicit acquire, renew and release of claims (S-6.17, D-5.1).

The rules live in `axcommon.ops.ClaimStore`; this module only supplies what
the store leaves to its caller:

- persistence: `claims.json` in intake's own state directory (D-5.1). A file
  that cannot be read is never overwritten; the destination answers 503
  `unavailable` until it is repaired (S-6.17).
- timeline: every `claim.*` event is written with the same ID the store puts
  into `claim_event_id`, so S-24.1 tools can cite it.
- identity: the owner comes from the receptacle-verified `Ident` and the
  instruction's `session`; payload claims about identity are not read.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import threading
from typing import Any

from axcommon.ops import ClaimStore

log = logging.getLogger("axintake.dest.b12b3.claim")

DESTINATION = "claim.op"
FALLBACK_LEASE_MIN = 30
FALLBACK_MAX_HOLD_H = 4


def claims_path() -> Path:
    base = os.environ.get("AX_STATE_DIR") or "/state"
    return Path(base) / "intake" / "claims.json"


class ClaimFile(dict):
    """A JSON file holding `{target_key: record}`; written whole after each operation."""

    def __init__(self, path: str | os.PathLike | None):
        super().__init__()
        self.path = Path(path) if path else None
        self.broken = False
        self.reload()

    def reload(self) -> None:
        self.clear()
        self.broken = False
        if self.path is None or not self.path.exists():
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.broken = True
            return
        if not isinstance(doc, dict) or not all(isinstance(v, dict) for v in doc.values()):
            self.broken = True
            return
        self.update(doc)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(dict(self), ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


def _limits() -> tuple[timedelta, timedelta]:
    try:
        from axcommon.defaults import Defaults

        d = Defaults.load()
        return timedelta(minutes=int(d.get("claim_lease_min"))), timedelta(hours=int(d.get("claim_max_hold_h")))
    except Exception:  # noqa: BLE001  missing defaults must not disable claims
        log.warning("claim_lease_min / claim_max_hold_h を読めない。既定値を使う")
        return timedelta(minutes=FALLBACK_LEASE_MIN), timedelta(hours=FALLBACK_MAX_HOLD_H)


def _unavailable(message: str) -> tuple[int, dict]:
    return 503, {"error": {"code": "unavailable", "message": message}}


class ClaimDestination:
    """The `claim.op` handler bound to one store and one claims file."""

    def __init__(self, app: Any, *, path: str | os.PathLike | None = None, now=None,
                 lease: timedelta | None = None, max_hold: timedelta | None = None, event_id=None):
        self.app = app
        self.file = ClaimFile(claims_path() if path is None else path)
        if lease is None or max_hold is None:
            default_lease, default_hold = _limits()
            lease = lease or default_lease
            max_hold = max_hold or default_hold
        kw = {"event_id": event_id} if event_id is not None else {}
        self.store = ClaimStore(self.file, now=now, lease=lease, max_hold=max_hold, emit=self._emit, **kw)
        self._lock = threading.Lock()
        self._ident = None
        self._session: str | None = None

    def _emit(self, event: dict) -> None:
        sink = getattr(getattr(self.app.state, "intake", None), "sink", None)
        if sink is None:
            return
        owner = event.get("owner") or {}
        target_key = event.get("target_key", "")
        data: dict[str, Any] = {"target_key": target_key, "principal": owner.get("principal"),
                                "session": owner.get("session"), "via": owner.get("via")}
        if event["kind"] in ("claim.granted", "claim.renewed"):
            record = self.file.get(target_key) or {}
            if record.get("lease_until"):
                data["lease_expires_at"] = record["lease_until"]
        if "by" in event:
            data["by"] = deepcopy(event["by"])
        source_extra: dict[str, Any] = {}
        if self._ident is not None:
            source_extra = {"principal": self._ident.principal, "via": self._ident.via}
        if self._session:
            source_extra["session"] = self._session[:64]
        try:
            sink.emit(event["kind"], target_key, data, event_id=event["id"], source_extra=source_extra or None)
        except Exception:  # noqa: BLE001  the timeline sink queues; a build error must not undo the claim
            log.exception("事象 %s を作れない", event["kind"])

    def _metadata(self, target: Any, declared: Any) -> dict:
        """Use only authenticated transport declarations and a ledger-verified target."""
        from urllib.parse import quote
        from axcommon.redact import from_environment

        metadata = {}
        if isinstance(declared, dict):
            try:
                redactor = from_environment()
                if redactor.index_ok and redactor.pii_list_ok:
                    for field in ("agent", "model"):
                        value = declared.get(field)
                        if isinstance(value, str) and 0 < len(value) <= 128 and not redactor.scan(value):
                            metadata[field] = value
            except Exception:
                pass  # Metadata is optional and never prevents a claim.
        if isinstance(target, str) and target.startswith("card:") and target[5:]:
            try:
                client = self.app.state.b12_read_relay.clients["ledger"]
                todo = client.get("/v1/todos/" + quote(target[5:], safe=""))
                if (isinstance(todo, dict) and todo.get("id") == target[5:]
                        and isinstance(todo.get("project"), str) and todo["project"]):
                    metadata["project"] = todo["project"]
            except Exception:
                pass  # Unverified project stays unknown.
        return metadata

    def __call__(self, app: Any, dest: dict, body: dict, ident: Any) -> tuple[int, Any]:
        return self.handle_with_context(app, dest, body, ident, {})

    def handle_with_context(self, app: Any, dest: dict, body: dict, ident: Any, declared: dict) -> tuple[int, Any]:
        payload = body.get("payload")
        if not isinstance(payload, dict):
            return 422, {"error": {"code": "bad_request", "message": "payload must be an object"}}
        session = body.get("session") if isinstance(body.get("session"), str) else None
        target = payload.get("target_key")
        metadata = self._metadata(target, declared) if payload.get("op") == "acquire" else {}
        with self._lock:
            if self.file.broken:
                self.file.reload()  # repaired by hand since the last call?
                if self.file.broken:
                    return _unavailable("claim store is unreadable")
            snapshot = deepcopy(dict(self.file))
            self._ident, self._session = ident, session
            try:
                decision = self.store.claim_op(payload, ident, session=session)
            finally:
                self._ident, self._session = None, None
            record = self.file.get(target) if isinstance(target, str) else None
            previous = snapshot.get(target) if isinstance(target, str) else None
            if (decision.rejection is None and payload.get("op") == "acquire" and record is not None
                    and (previous is None or previous.get("acquired_at") != record.get("acquired_at"))):
                record["metadata"] = metadata
            try:
                self.file.save()
            except OSError:
                self.file.clear()
                self.file.update(snapshot)
                log.error("保持の保存先に書けない。操作を戻す")
                return _unavailable("claim store is not writable")
        if decision.rejection is not None:
            r = decision.rejection
            return r.status, {"error": {"code": r.reason, "message": r.message}, "data": deepcopy(dict(r.data))}
        return 200, deepcopy(dict(decision.data))


def register(app, **kw) -> ClaimDestination:
    handler = ClaimDestination(app, **kw)
    state = app.state.intake
    state.dest_handlers[DESTINATION] = handler
    # The store is exposed so a later conflict guard shares one claims table (S-6.17).
    state.claim_store = handler.store
    return handler

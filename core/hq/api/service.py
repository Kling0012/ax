"""Read models and authenticated intake submission for S-12/S-22.

Only IntakeClient submits mutations. Readers never write to ledger or pool.
"""
from __future__ import annotations

import re
import threading
from datetime import date, timedelta, timezone
from urllib.parse import quote

from axcommon import schemas, timeutil
from axcommon.client import ApiError, LedgerClient, Unavailable, service_url
from axcommon.creds import read_secret
from axcommon.defaults import Defaults
from core.hq.notify.intake import IntakeClient, IntakeError
from core.intake.registry import Registry, payload_contract, repo_config_dir, unresolved_contract

JST = timezone(timedelta(hours=9))
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MUTATIONS = frozenset({"work.request", "ledger.answer", "work.stop", "work.cancel", "work.resume",
                       "schedule.update", "life_state.update", "goal.update", "commitment.update"})


class ContractMissing(NotImplementedError):
    pass


class InvalidRequest(ValueError):
    pass


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise InvalidRequest("invalid_id")
    return quote(value, safe="")


def day(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise InvalidRequest("invalid_date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise InvalidRequest("invalid_date") from None


class MissingLedger:
    def get(self, *args, **kwargs):
        raise Unavailable("hq_credential_unavailable")


class BusinessAPI:
    def __init__(self, ledger, *, intake=None, defaults=None, now=None, redactor=None, registry=None):
        self.ledger = ledger
        self.intake = intake or IntakeClient()
        self.defaults = defaults or Defaults.load()
        self.now = now or timeutil.now
        self.redactor = redactor
        self.registry = registry if registry is not None else Registry.load(repo_config_dir())
        self._last_success: dict[str, str] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, *, redactor=None):
        try:
            ledger = LedgerClient(service_url("ledger"), token=read_secret("core-token-hq"))
        except Exception:
            ledger = MissingLedger()
        return cls(ledger, redactor=redactor)

    def clean(self, data):
        # A missing scanner must not expose uninspected text.
        if self.redactor is None:
            raise Unavailable("redaction_unavailable")
        return self.redactor.redact_obj(data)[0]

    def read(self, path: str, *, params=None, collection=None) -> tuple[int, dict]:
        key = path + repr(sorted((params or {}).items()))
        try:
            data = self.ledger.get(path, params=params)
            if not isinstance(data, dict) or (collection is not None and not isinstance(data.get(collection), list)):
                raise ValueError("invalid_ledger_response")
            data = self.clean(data)
            ts = timeutil.format_ts(self.now())
            with self._lock:
                self._last_success[key] = ts
            return 200, {"ok": True, "data": data, "error": None, "sample": False, "last_success_at": ts}
        except ApiError as exc:
            status, code = exc.status, "not_found" if exc.status == 404 else "fetch_failed"
        except Exception:
            status, code = 503, "fetch_failed"
        with self._lock:
            previous = self._last_success.get(key)
        return status, {"ok": False, "data": None, "error": {"code": code},
                        "sample": False, "last_success_at": previous}

    def submit(self, destination: str, payload: dict, op_id: str) -> tuple[int, dict]:
        if destination not in MUTATIONS:
            raise InvalidRequest("destination_not_allowed")
        if not isinstance(op_id, str) or not op_id.strip() or len(op_id) > 120:
            raise InvalidRequest("op_id_required")
        self.registry.maybe_reload()
        entry = self.registry.get(destination)
        if unresolved_contract(entry):
            raise ContractMissing(destination)
        schema = payload_contract(entry, schemas.load(f"dest/{destination}.json"))
        if schema.get("x-ax-unresolved"):
            raise ContractMissing(destination)
        if schemas.errors(f"dest/{destination}.json", payload):
            raise InvalidRequest("payload_schema")
        # Ensure responses can be inspected before causing a side effect.
        self.clean({})
        try:
            status, result = self.intake.submit(destination, payload, idempotency_key=f"hq-api:{destination}:{op_id}")
        except (IntakeError, ConnectionError):
            return 503, {"ok": False, "error": {"code": "intake_unavailable"}, "preserve_input": True}
        if not isinstance(result, dict) or not result:
            return 502, {"ok": False, "error": {"code": "invalid_intake_response"}, "preserve_input": True}
        # A transport acknowledgment is not confirmation of a state change.
        result = self.clean(result)
        out = {"ok": status < 400 and result.get("ok", True) is not False,
               "receipt": result, "preserve_input": True, "confirmed": False}
        if destination == "work.request":
            nested = result.get("result")
            todo_id = nested.get("todo_id") if isinstance(nested, dict) else result.get("todo_id")
            if todo_id is not None:
                identifier(todo_id)
                out.update(todo_id=todo_id, todo_ref=f"#/todo/{quote(todo_id, safe='')}",
                           todo_api=f"/v1/todos/{quote(todo_id, safe='')}")
            elif out["ok"] and status != 202:
                out.update(ok=False, error={"code": "todo_reference_missing"})
                status = 502
        if destination == "ledger.answer" and out["ok"] and status != 202:
            try:
                todo = self.ledger.get(f"/v1/todos/{identifier(payload['todo_id'])}")
                if not isinstance(todo, dict) or todo.get("state") != "todo":
                    raise ValueError()
                out.update(confirmed=True, todo=self.clean(todo))
            except Exception:
                out.update(ok=False, error={"code": "answer_unconfirmed"})
                status = 503
        return status, out

    def unfinished(self, items: list[dict]) -> dict:
        checked = []
        for item in items:
            todo_id = item.get("todo_id") if isinstance(item, dict) else None
            try:
                path_id = identifier(todo_id)
            except InvalidRequest:
                checked.append({"todo_id": None, "ok": False, "reason": "todo_id_required"})
                continue
            status, result = self.read(f"/v1/todos/{path_id}")
            todo = result.get("data")
            reason = None
            if status != 200:
                reason = "todo_not_found" if status == 404 else "ledger_unavailable"
            elif todo.get("state") in {"done", "dropped"}:
                reason = "todo_closed"
            elif todo.get("state") not in {"todo", "running", "waiting_user", "parked", "review"}:
                reason = "todo_state_unknown"
            checked.append({"todo_id": todo_id, "ok": reason is None, "reason": reason})
        return {"ok": all(row["ok"] for row in checked), "items": checked}

    def digest(self, target_date: str, notify) -> tuple[int, dict]:
        day(target_date)
        try:
            if notify is None:
                raise Unavailable()
            row = notify.store.digest_get(target_date)
            notification = None
            if row and row.get("notification_id"):
                raw = notify.store.get(row["notification_id"])
                if raw is None:
                    raise Unavailable()
                notification = {k: raw.get(k) for k in ("id", "title", "message", "state", "last_error", "sent_at")}
            return 200, {"ok": True, "data": self.clean({"delivery": row, "notification": notification}),
                         "error": None, "sample": False}
        except Exception:
            return 503, {"ok": False, "data": None, "error": {"code": "digest_unavailable"}, "sample": False}

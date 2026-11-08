"""Forward ledger updates through the authenticated core API."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from axcommon import creds, schemas
from axcommon.client import ApiClient, ApiError, Unavailable, service_url

KINDS = {"commitment.update": "promises", "goal.update": "goals", "schedule.update": "schedules"}


def register(app):
    for name in (*KINDS, "life_state.update"):
        app.state.intake.dest_handlers[name] = handle


def _error(status, code, message):
    return status, {"error": {"code": code, "message": message}}


def _token():
    path = os.environ.get("AX_CORE_TOKEN_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return creds.read_secret_optional("core-token-intake") or None


def handle(app, dest, body, ident):
    name = dest.get("name")
    if name not in (*KINDS, "life_state.update"):
        return _error(422, "payload_schema", "Unknown destination")
    payload = body.get("payload")
    errors = schemas.errors(f"dest/{name}.json", payload)
    if errors:
        return _error(422, "payload_schema", "; ".join(errors[:3]))
    token = _token()
    if not token:
        return _error(503, "ledger_unavailable", "Ledger credentials unavailable")
    data = dict(payload)
    if name == "life_state.update":
        kind = data.pop("kind")
        path = "/v1/focus-state" if kind == "focus" else "/v1/daily-state/" + quote(data["date"], safe="")
        method = "PUT"
    else:
        op = data.pop("op")
        path = "/v1/" + KINDS[name]
        if op == "create":
            method = "POST"
        else:
            path += "/" + quote(data.pop("id"), safe="")
            method = "PATCH" if op == "update" else "POST"
            if op == "transition":
                path += "/transition"
    client = ApiClient(service_url("ledger"), token=token, transport=app.state.intake.transport)
    try:
        return 200, client.request(method, path, json_body=data)
    except ApiError as exc:
        return exc.status, exc.body if isinstance(exc.body, dict) and exc.body else {"error": {"code": exc.code, "message": exc.message}}
    except (Unavailable, OSError):
        return _error(503, "ledger_unavailable", "Ledger unavailable")

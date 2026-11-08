"""ToDo transitions from the authenticated intake receptacle."""
from urllib.parse import quote

from axcommon import schemas
from axcommon.client import ApiClient, ApiError
from core.intake.dest.b10a.handler import _token
from core.intake.vias import Ident


def _handle(app, dest, body, ident: Ident, state):
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return 422, {"error": {"code": "payload_schema", "message": "invalid payload"}}
    name = "ledger.complete" if state == "done" else "ledger.park"
    errors = schemas.errors("dest/" + name + ".json", payload)
    if errors:
        return 422, {"error": {"code": "payload_schema", "message": "; ".join(errors[:3])}}
    if ident.via not in ("hq", "local", "ssh", "tailnet") or (ident.via == "hq") != (ident.from_ == "hq"):
        return 403, {"error": {"code": "forged_authority", "message": "invalid origin"}}
    from core.intake.app import api_from_handler
    client, path = api_from_handler(dest.get("handler", "").replace("{todo_id}", quote(payload["todo_id"], safe="")), transport=app.state.intake.transport)
    client = ApiClient(client.base_url, token=_token(), transport=app.state.intake.transport, extra_headers={"X-AX-Via": ident.via})
    transition = {"to": state}
    for key in ("reason", "check_at"):
        if key in payload:
            transition[key] = payload[key]
    try:
        return 200, client.post(path, transition)
    except ApiError as exc:
        return exc.status, exc.body if isinstance(exc.body, dict) and exc.body else {"error": {"code": exc.code, "message": exc.message}}


def complete(app, dest, body, ident):
    return _handle(app, dest, body, ident, "done")


def park(app, dest, body, ident):
    return _handle(app, dest, body, ident, "parked")


def register(app):
    app.state.intake.dest_handlers.update({"ledger.complete": complete, "ledger.park": park})

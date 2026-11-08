"""Record commander decisions through the ledger's intake credential."""
from axcommon import schemas
from axcommon.client import ApiClient, ApiError, Unavailable, service_url
from core.intake.dest.b10a.handler import _token


def handle(app, dest, body, ident):
    payload = body.get("payload")
    errors = schemas.errors("dest/decision.record.json", payload)
    if errors:
        return 422, {"error": {"code": "payload_schema", "message": "; ".join(errors[:3])}}
    if ident.via == "hq" and (ident.from_ != "hq" or ident.principal != "user"):
        return 403, {"error": {"code": "forged_authority"}}
    token = _token()
    if not token:
        return 503, {"error": {"code": "auth_unavailable"}}
    recorded_by = {"principal": ident.principal}
    if body.get("session"):
        recorded_by["session"] = body["session"]
    request = {**payload, "recorded_by": recorded_by}
    try:
        client = ApiClient(service_url("ledger"), token=token,
                           transport=app.state.intake.transport,
                           extra_headers={"X-AX-Via": ident.via})
        result = client.post("/v1/decisions", request)
    except ApiError as exc:
        return exc.status, exc.body if isinstance(exc.body, dict) else {"error": {"code": exc.code}}
    except (Unavailable, OSError):
        return 503, {"error": {"code": "ledger_unavailable"}}
    return 200, result

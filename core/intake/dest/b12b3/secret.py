"""Slot-only destination for remote secret delivery."""

import re

_SLOT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}\Z")


def handle(app, dest, body, ident):
    if getattr(ident, "from_", None) != "hq":
        return 403, {"error": {"code": "forbidden", "message": "HQ required"}}
    payload = body.get("payload") if isinstance(body, dict) else None
    if (not isinstance(payload, dict) or set(payload) != {"slot"}
            or not isinstance(payload["slot"], str) or not _SLOT.fullmatch(payload["slot"])):
        return 422, {"error": {"code": "payload_schema", "message": "invalid secret request"}}
    push = getattr(app.state.intake, "secret_push", None)
    if not callable(push):
        from axcommon.client import ApiClient, service_url
        from core.intake.dest.b10a.handler import _token
        def push(slot):
            token = _token()
            if not token:
                return False
            result = ApiClient(service_url("resources"), token=token,
                               transport=app.state.intake.transport, timeout=120).post("/v1/secret-push", {"slot": slot})
            return isinstance(result, dict) and result.get("ok") is True
    try:
        delivered = push(payload["slot"])
    except Exception:
        return 503, {"error": {"code": "secret_push_failed", "message": "secret delivery failed"}}
    if delivered is not True:
        return 503, {"error": {"code": "secret_push_failed", "message": "secret delivery failed"}}
    return 200, {"delivered": True}

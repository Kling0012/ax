"""Seat destination adapters; registration and HQ delivery are injected at integration."""
from __future__ import annotations

from axcommon import schemas, timeutil
from core.intake.seat.store import SeatError, digest


def _error(status, code):
    return status, {"error": {"code": code, "message": code}}


def register(app):
    handlers = app.state.intake.dest_handlers
    for name in ("seat.enroll_start", "seat.lock", "seat.tier", "seat.unlock"):
        handlers[name] = handle


def handle(app, dest, body, ident):
    name = dest.get("name") or body.get("destination")
    if name not in {"seat.enroll_start", "seat.lock", "seat.tier", "seat.unlock"}:
        return _error(404, "unknown_destination")
    if (getattr(ident, "via", None), getattr(ident, "from_", None),
            getattr(ident, "principal", None)) != ("hq", "hq", "user"):
        return _error(403, "forged_authority")
    approval = body.get("approved_via")
    if not isinstance(approval, str) or not approval:
        return _error(403, "forged_authority")
    payload = body.get("payload")
    if schemas.errors("dest/" + name + ".json", payload):
        return _error(422, "bad_request")
    service = getattr(app.state, "seats", None)
    if service is None:
        return _error(503, "seat_service_unavailable")
    try:
        if name == "seat.enroll_start":
            deliver = getattr(app.state, "seat_enrollment_delivery", None)
            if not callable(deliver):
                return _error(503, "seat_enrollment_delivery_unavailable")
            code = service.start_enrollment(payload, approval)
            # The trusted integration must deliver directly to the requesting HQ
            # session, once, without logging or persistence of the code.
            try:
                deliver(approval, code)
            except Exception:
                # A delivery failure may have exposed the code. Invalidate it
                # even if the recipient's outcome is unknown.
                with service.store.lock, service.store._database():
                    service.store.db.execute(
                        "UPDATE enroll_codes SET used_at=? WHERE code_digest=? AND used_at IS NULL",
                        (timeutil.now_ts(), digest(code)),
                    )
                return _error(503, "seat_enrollment_delivery_failed")
            return 200, {"state": "issued"}
        if name in {"seat.lock", "seat.unlock"}:
            service.set_lock(ident, payload["scope"], name == "seat.lock", approved=True)
            return 200, {"scope": payload["scope"], "locked": name == "seat.lock"}
        with service.store.lock:
            row = service.store.get(payload["seat_id"])
            if row["state"] == "revoked":
                return _error(409, "seat_revoked")
            if "expires_at" in payload:
                expiry = timeutil.parse_ts(payload["expires_at"])
                if expiry <= timeutil.now() or expiry < timeutil.parse_ts(row["expires_at"]):
                    return _error(422, "invalid_expiry")
            row.update({key: payload[key] for key in ("tier", "expires_at") if key in payload})
            row = service.store.put(row)
            if "expires_at" in payload:
                service.store.set_policy("expired:" + row["seat_id"], False)
        service.emit("seat.tier_changed", row["seat_id"])
        return 200, {key: row[key] for key in ("seat_id", "tier", "expires_at")}
    except SeatError as error:
        return _error(error.status, error.reason)
    except (KeyError, TypeError, ValueError):
        return _error(422, "bad_request")
    except Exception:
        return _error(503, "seat_operation_unavailable")

"""Request construction for a commander's own seat revocation."""
from __future__ import annotations


def build_request(seat_id, request_id, *, session=None):
    if not isinstance(seat_id, str) or not seat_id:
        raise ValueError("seat_id is required")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        raise ValueError("request_id is required")
    body = {"from": "commander", "destination": "seat.revoke",
            "payload": {"seat_id": seat_id}, "idempotency_key": request_id}
    if session:
        body["session"] = session
    return "POST", "/v1/instructions", body, request_id

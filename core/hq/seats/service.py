"""All seat changes use intake. No seat-file, gate or host access."""
from __future__ import annotations

from dataclasses import dataclass
import secrets

from axcommon import schemas
from core.hq.approval.service import ApprovalFailure, VerifiedAction

FACTOR_OPERATIONS = frozenset({"seat.add", "seat.enroll_start", "seat.tier", "seat.unlock"})
OPERATIONS = FACTOR_OPERATIONS | {"seat.revoke", "seat.lock"}


@dataclass(frozen=True)
class RegistrationDisplay:
    """Trusted response adapter maps the intake wire schema to this local DTO."""
    enrollment_code: str | None = None
    key_fp: str | None = None


class SeatService:
    def __init__(self, intake, *, registration_display=None):
        self.intake = intake
        self.registration_display = registration_display or self._registration_display

    @staticmethod
    def _registration_display(destination, result):
        if destination == "seat.enroll_start":
            return RegistrationDisplay(enrollment_code=result.get("enrollment_code"))
        return RegistrationDisplay()

    def prepare(self, destination, payload):
        if destination not in OPERATIONS or not isinstance(payload, dict):
            raise ApprovalFailure(422, "bad_request")
        schema = schemas.load("dest/" + destination + ".json")
        if schema.get("x-ax-unresolved"):
            raise NotImplementedError(schema["x-ax-unresolved"])
        try:
            schemas.validate("dest/" + destination + ".json", payload)
        except schemas.ValidationFailed:
            raise ApprovalFailure(422, "bad_request") from None
        if destination in {"seat.add", "seat.enroll_start"} and self.registration_display is None:
            raise NotImplementedError("S-22.10: seat registration response binding missing")
        return {"destination": destination, "payload": payload, "request_id": "hq-seat:" + secrets.token_hex(24)}

    def apply(self, saved, proof: VerifiedAction):
        destination, payload = saved["destination"], saved["payload"]
        if destination in FACTOR_OPERATIONS and not proof.second_factor:
            raise ApprovalFailure(403, "second_factor_required")
        status, result = self.intake.submit(destination, payload, proof, request_id=saved["request_id"])
        data = result.get("data", result)
        if status == 202:
            # The same human action includes the request and the approval. Only
            # grant if intake's saved contents match exactly what was displayed.
            aid = data.get("approval_id")
            if not aid or not proof.second_factor:
                raise ApprovalFailure(502, "saved_seat_request_missing")
            view = self.intake.approval(aid, proof)
            row = view.get("data", view)
            if row.get("destination") != destination or row.get("payload") != payload:
                raise ApprovalFailure(409, "approval_mismatch")
            status, result = self.intake.grant(aid, row["digest"], proof)
        display = RegistrationDisplay()
        if destination in {"seat.add", "seat.enroll_start"}:
            display = self.registration_display(destination, result)
            if not isinstance(display, RegistrationDisplay):
                raise ApprovalFailure(502, "registration_bad_response")
            if destination == "seat.enroll_start" and not display.enrollment_code:
                raise ApprovalFailure(502, "registration_bad_response")
        return status, display

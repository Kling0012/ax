"""HQ-only Unix intake adapter; undefined proof wire format fails closed."""
from __future__ import annotations

import json
from urllib.parse import quote

from axcommon import ids
from core.hq.notify.intake import unix_transport
from .service import ApprovalFailure, VerifiedAction


class UnixIntake:
    def __init__(self, *, transport=None, proof_encoder=None):
        self.transport = transport or unix_transport()
        # encoder(method, path, body, verified) -> body, supplied together with
        # intake's hq_verifier. Not inferred from boolean claims in client JSON.
        self.proof_encoder = proof_encoder

    def _call(self, method, path, body=None, proof=None):
        from axcommon.hq_proof import KeyUnavailable, requires_proof
        protected = method != "GET" and (path != "/v1/instructions" or requires_proof((body or {}).get("destination")))
        if protected and self.proof_encoder is None:
            raise NotImplementedError("S-6.10: HQ verification-result wire binding missing")
        try:
            encoded = self.proof_encoder(method, path, body, proof) if protected else body
        except KeyUnavailable:
            raise ApprovalFailure(503, "proof_key_unavailable") from None
        except (ValueError, OSError):
            raise ApprovalFailure(503, "approval_unavailable") from None
        headers = None
        if isinstance(encoded, tuple):
            encoded, headers = encoded
        raw = None if encoded is None else json.dumps(encoded, ensure_ascii=False).encode("utf-8")
        if headers is None:
            status, data = self.transport(method, path, raw)
        else:
            status, data = self.transport(method, path, raw, headers)
        try:
            value = json.loads(data) if data else {}
        except ValueError:
            raise ApprovalFailure(502, "intake_bad_response") from None
        if status >= 400:
            # Never reflect upstream messages or payloads that may contain codes.
            code = (value.get("error") or {}).get("code") if isinstance(value, dict) else None
            raise ApprovalFailure(status, "proof_key_unavailable" if code == "proof_key_unavailable" else "intake_rejected")
        if not isinstance(value, dict):
            raise ApprovalFailure(502, "intake_bad_response")
        return status, value

    def approval_snapshot(self, proof: VerifiedAction):
        return self._call("GET", "/v1/approval-snapshot", proof=proof)[1]

    def approvals(self, proof: VerifiedAction):
        return self._call("GET", "/v1/approvals", proof=proof)[1]

    def approval(self, approval_id, proof):
        return self._call("GET", "/v1/approvals/" + quote(approval_id, safe=""), proof=proof)[1]

    def grant(self, approval_id, digest, proof):
        return self._call("POST", "/v1/approvals/" + quote(approval_id, safe="") + "/grant",
                          {"digest": digest}, proof)

    def deny(self, approval_id, digest, proof, *, request_id):
        return self.submit("approval.deny", {"approval_id": approval_id, "digest": digest}, proof,
                           request_id=request_id)

    def seats(self, proof):
        return self._call("GET", "/v1/seats", proof=proof)[1]

    def submit(self, destination, payload, proof, *, request_id):
        body = {"id": ids.new_ulid(), "from": "hq", "destination": destination,
                "payload": payload, "idempotency_key": request_id}
        return self._call("POST", "/v1/instructions", body, proof)

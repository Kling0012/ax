"""Saved-content execution; HQ verifies the second factor, intake never sees codes."""
from __future__ import annotations

from dataclasses import dataclass
import html
import json
from typing import Callable

from axcommon import ids, timeutil
from core.intake.vias import Ident
from .audit import ApprovalAudit
from .go import GoConfirm
from .policy import AuthorityPolicy, external_view
from .store import ApprovalError, ApprovalStore, canonical_json, payload_digest


@dataclass(frozen=True)
class VerifiedUser:
    """Adapter result from authenticated HQ, not an instruction payload."""
    tailnet_allowed: bool
    second_factor: bool
    direct_tailnet: bool
    subject: str = "user"


class ApprovalService:
    def __init__(self, store: ApprovalStore, *, policy: AuthorityPolicy, executor: Callable | None = None,
                 audit: ApprovalAudit | None = None, go: GoConfirm | None = None,
                 card_writer: Callable | None = None, notifier: Callable | None = None,
                 ttl_min: float = 60):
        self.store, self.policy, self.executor = store, policy, executor
        self.audit = audit or ApprovalAudit()
        self.go = go or GoConfirm(store)
        self.card_writer, self.notifier = card_writer, notifier
        self.ttl_min = ttl_min
        if ttl_min <= 0:
            raise ValueError("approval_ttl_min")

    def _event(self, kind: str, row: dict, ident: Ident, **extra):
        data = {k: row[k] for k in ("approval_id", "destination", "digest", "requested_by")}
        if row.get("granted_at"):
            data["granted_by"] = json.loads(row["result_json"]).get("granted_by", ident.principal)
        data.update(extra)
        self.audit.emit(kind, row["approval_id"], ident, session=row["session"], project=row["project"], data=data)

    def submit(self, body: dict, dest: dict, ident: Ident) -> dict:
        effective = self.policy.check(body, dest, ident)
        if effective["tier"] != "irreversible":
            raise ApprovalError(422, "approval_not_required")
        # Re-evaluate via trusted policy; permission.change itself cannot bypass.
        if self.policy.permitted(body, ident):
            raise ApprovalError(409, "standing_permission_applies")
        payload = json.loads(canonical_json(body["payload"]))
        derived = self.go.validate(payload) if dest["name"] == "go.confirm" else None
        # Store no secrets in payloads, explanations or metadata.
        if self.audit.redactor.scan(canonical_json(payload)):
            raise ApprovalError(422, "scan_blocked")
        for key in ("session", "project", "idempotency_key"):
            value = body.get(key)
            if value is not None and (not isinstance(value, str) or self.audit.redactor.scan(value)):
                raise ApprovalError(422, "scan_blocked")
        expires = timeutil.add_seconds(timeutil.now_ts(), self.ttl_min * 60)
        if body.get("expires_at") and timeutil.parse_ts(body["expires_at"]) < timeutil.parse_ts(expires):
            expires = timeutil.format_ts(timeutil.parse_ts(body["expires_at"]))
        if dest.get("default_expiry_min"):
            default_exp = timeutil.add_seconds(timeutil.now_ts(), dest["default_expiry_min"] * 60)
            expires = min(expires, default_exp)
        request_key = body["idempotency_key"]
        fingerprint = payload_digest({"destination": dest["name"], "payload": payload,
                                      "project": body.get("project"), "expires_at": body.get("expires_at")})
        with self.store.transaction():
            for prior in self.store.list():
                saved = json.loads(prior["result_json"])
                if (prior["destination"] == dest["name"] and prior["requested_by"] == ident.principal
                        and saved.get("request_key") == request_key):
                    if saved.get("request_fingerprint") != fingerprint:
                        raise ApprovalError(409, "idempotency_conflict")
                    return {"approval_id": prior["approval_id"], "status": prior["state"], "replayed": True}
            result = {"request_key": request_key, "request_fingerprint": fingerprint,
                      "caller": self.audit.caller(ident), "card_pending": True}
            if derived is not None:
                result["derived"] = derived
            row = {"approval_id": ids.new_ulid(), "destination": dest["name"], "payload_json": canonical_json(payload),
                   "digest": payload_digest(payload), "requested_by": ident.principal, "session": body.get("session"),
                   "project": body.get("project"), "created_at": timeutil.now_ts(), "expires_at": expires,
                   "state": "pending_user", "granted_at": None, "executed_at": None, "result_json": canonical_json(result)}
            self.store.insert(row)
        self._event("approval.requested", row, ident)
        self.deliver_card(row["approval_id"])
        return {"approval_id": row["approval_id"], "status": "pending_user"}

    def deliver_card(self, approval_id: str):
        row = self.store.get(approval_id)
        result = json.loads(row["result_json"])
        if not result.get("card_pending") or row["state"] != "pending_user" or not self.card_writer:
            return
        view = self.view(approval_id)
        # Deterministic ID allows a retry after an ambiguous ledger response.
        card = {"id": approval_id, "title": "戻しにくい操作の承認", "state": "waiting_user", "owner": "user",
                "kind": "approval", "project": row["project"], "user_only": True,
                "card": {"question": "保存した内容を承認するか", "waiting_reason": "irreversible", "user_only": True,
                         "options": ["承認", "却下"], "approval_id": approval_id, "approval": view}}
        try:
            self.card_writer(card)
            if self.notifier:
                self.notifier({"kind": "approval_pending", "approval_id": approval_id})
        except Exception:
            return  # Kept in result_json for retry; no duplicate external message is retried blindly.
        with self.store.transaction():
            latest = self.store.get(approval_id)
            result = json.loads(latest["result_json"])
            result["card_pending"] = False
            self.store.update(approval_id, result_json=canonical_json(result))

    def pending_snapshot(self) -> list[dict]:
        """Read saved pending approvals without refreshing or writing audit events."""
        now = timeutil.now()
        return [{"approval_id": row["approval_id"], "destination": row["destination"],
                 "state": "expired" if timeutil.parse_ts(row["expires_at"]) <= now else "pending_user",
                 "expires_at": row["expires_at"]}
                for row in self.store.list() if row["state"] == "pending_user"]

    def view(self, approval_id: str) -> dict:
        self.refresh(approval_id)
        row = self.store.get(approval_id)
        result = json.loads(row["result_json"])
        payload = json.loads(row["payload_json"])
        view = {k: row[k] for k in ("approval_id", "destination", "digest", "requested_by", "session", "project",
                                   "created_at", "expires_at", "state", "granted_at", "executed_at")}
        view.update(payload=payload, digest_short=row["digest"][:8], caller=result.get("caller"),
                    granted_by=result.get("granted_by"), derived=result.get("derived"), go=result.get("go"),
                    explanation={"label": "司令塔の説明（未検証）", "text": payload.get("reason", ""),
                                 "html": html.escape(payload.get("reason", ""))})
        return external_view(view, ["payload.reason", "explanation.text"])

    @staticmethod
    def _hq(ident: Ident, proof: VerifiedUser | None = None, *, factor=True):
        if not isinstance(ident, Ident) or (ident.via, ident.from_) != ("hq", "hq"):
            raise ApprovalError(403, "receptacle_not_allowed")
        if not isinstance(proof, VerifiedUser):
            raise ApprovalError(403, "unauthorized")
        if not proof.direct_tailnet:
            raise ApprovalError(404, "not_found")
        if not proof.tailnet_allowed:
            raise ApprovalError(403, "peer_unknown")
        if factor and not proof.second_factor:
            raise ApprovalError(403, "bad_code")

    def refresh(self, approval_id: str):
        with self.store.transaction():
            row = self.store.get(approval_id)
            if row["state"] != "pending_user":
                return
            result = json.loads(row["result_json"])
            caller = result.get("caller", {})
            ident = (Ident("hq", "hq", "component:change", "irreversible") if row["destination"] == "change.approval.record"
                     else Ident(caller.get("via", "local"), "commander", row["requested_by"], caller.get("tier", "irreversible")))
            reason = self.policy.seat_check(ident) if self.policy.seat_check else None
            if reason == "seat_revoked":
                state, event, reason = "denied", "approval.denied", "requester_revoked"
            elif timeutil.parse_ts(row["expires_at"]) <= timeutil.now():
                state, event, reason = "expired", "approval.expired", "expired"
            else:
                return
            result["reason"] = reason
            self.store.update(approval_id, state=state, result_json=canonical_json(result))
        self._event(event, row, ident, reason=reason)

    def grant(self, approval_id: str, digest: str, ident: Ident, proof: VerifiedUser) -> dict:
        self._hq(ident, proof)
        self.refresh(approval_id)
        mismatch = None
        with self.store.transaction():
            row = self.store.get(approval_id)
            if row["state"] != "pending_user":
                raise ApprovalError(409, "approval_not_pending")
            if timeutil.parse_ts(row["expires_at"]) <= timeutil.now():
                raise ApprovalError(409, "approval_expired")
            result = json.loads(row["result_json"])
            caller = result["caller"]
            requester = (Ident("hq", "hq", "component:change", "irreversible") if row["destination"] == "change.approval.record"
                         else Ident(caller["via"], "commander", row["requested_by"], caller["tier"]))
            if self.policy.seat_check:
                reason = self.policy.seat_check(requester)
                if reason:
                    raise ApprovalError(503 if reason in {"locked", "lockdown"} else 403, reason)
            if not isinstance(digest, str) or digest != row["digest"] or payload_digest(json.loads(row["payload_json"])) != row["digest"]:
                mismatch = "approval.mismatch"
            else:
                if row["destination"] not in {"go.confirm", "change.approval.record"}:
                    supports = getattr(self.executor, "supports", None)
                    if self.executor is None or (callable(supports) and not supports(row["destination"])):
                        raise NotImplementedError("D-5.1: approved write credential/executor binding required")
                # Commit consumption before any side effect; a crash never causes replay.
                result["granted_by"] = ident.principal
                result["execution"] = "reserved"
                self.store.update(approval_id, state="granted", granted_at=timeutil.now_ts(), result_json=canonical_json(result))
        if mismatch:
            self._event(mismatch, row, ident)
            raise ApprovalError(409, "approval_mismatch")
        row = self.store.get(approval_id)
        self._event("approval.granted", row, ident)
        result = json.loads(row["result_json"])
        enrollment_code = None
        try:
            if row["destination"] == "go.confirm":
                result["go"] = self.go.execute(row)
            elif row["destination"] == "change.approval.record":
                pass  # A human grant records the decision, never executes the change.
            else:
                # Never use grant's payload or the commander's transport credentials.
                command = {"destination": row["destination"], "payload": json.loads(row["payload_json"]),
                           "project": row["project"], "session": row["session"], "expires_at": row["expires_at"],
                           "issued_by": "user", "approved_via": approval_id, "confirm_irreversible": True}
                response = self.executor(command)
                if isinstance(response, tuple) and response[0] >= 400:
                    raise ApprovalError(response[0], "approved_handler_failed")
                if row["destination"] == "seat.enroll_start" and isinstance(response, tuple):
                    value = response[1]
                    if isinstance(value, dict) and isinstance(value.get("enrollment_code"), str):
                        enrollment_code = value["enrollment_code"]
                # Do not retain a response that might echo credentials.
            result["execution"] = "completed"
        except Exception:
            result["execution"] = "uncertain"
            with self.store.transaction():
                self.store.update(approval_id, result_json=canonical_json(result))
            raise ApprovalError(503, "approved_execution_uncertain") from None
        with self.store.transaction():
            # A concurrent withdrawal prevents future use; it is never overwritten.
            latest = self.store.get(approval_id)
            self.store.update(approval_id, state="executed" if latest["state"] == "granted" else latest["state"],
                              executed_at=timeutil.now_ts(), result_json=canonical_json(result))
        row = self.store.get(approval_id)
        go_event = {k: result["go"][k] for k in ("kind", "scope", "changes", "go_md_sha256")} if "go" in result else None
        self._event("approval.executed", row, ident, **({"go": go_event} if go_event else {}))
        view = self.view(approval_id)
        if enrollment_code is not None:
            view["enrollment_code"] = enrollment_code
        return view

    def deny(self, approval_id: str, ident: Ident, proof: VerifiedUser):
        self._hq(ident, proof)
        return self._finish(approval_id, ident, "denied", "approval.denied")

    def withdraw(self, approval_id: str, ident: Ident):
        row = self.store.get(approval_id)
        if ident.principal != row["requested_by"] or ident.from_ != "commander":
            raise ApprovalError(403, "receptacle_not_allowed")
        if self.policy.seat_check and self.policy.seat_check(ident):
            raise ApprovalError(403, "seat_revoked")
        return self._finish(approval_id, ident, "withdrawn", "approval.withdrawn")

    def _finish(self, approval_id, ident, state, event):
        self.refresh(approval_id)
        with self.store.transaction():
            row = self.store.get(approval_id)
            go = row["destination"] == "go.confirm"
            if row["state"] != "pending_user" and not (go and row["state"] == "executed" and state == "withdrawn"):
                raise ApprovalError(409, "approval_not_pending")
            self.store.update(approval_id, state=state)
        self._event(event, row, ident)
        return self.view(approval_id)

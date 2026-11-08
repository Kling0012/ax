"""席の資格、失効と登録コードの判定。承認の保存と実行は b12b2 に委ねる。"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import json
import os
import re
import secrets
import sqlite3
import threading
from typing import Callable

from axcommon import ids, peercred, timeutil
from core.intake.vias import Ident, hq_ident, local_ident, local_principal, register_via_factory
from .store import SeatError, SeatStore, digest, key_fingerprint

READS = {"status", "todos", "events", "brief", "whoami", "destinations", "results", "show", "approvals", "seats", "decisions", "guide"}
CLEANUP = ("deny_pending", "release_controls", "release_claims", "close_sessions")
ENROLL_CODE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class RelayEvidence:
    """gate の内側の検査で得た事実。本文からは生成しない。"""
    core_token: str
    device_id: str
    device: str
    bearer: str = ""


def decode_relay_evidence(request: dict) -> RelayEvidence:
    """Decode only the gate-to-intake headers defined by the gate relay.

    ``scope[headers]`` is used when available so duplicated authority headers
    cannot become a last-value-wins ambiguity.
    """
    scope = request.get("scope") if isinstance(request, dict) else None
    raw = scope.get("headers", ()) if isinstance(scope, dict) else ()
    values: dict[str, list[str]] = {}
    for key, value in raw:
        try:
            name = key.decode("latin-1").lower() if isinstance(key, bytes) else str(key).lower()
            text = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        except UnicodeError:
            raise SeatError("unauthorized", 401) from None
        values.setdefault(name, []).append(text)
    if not values:
        supplied = request.get("headers", {}) if isinstance(request, dict) else {}
        if not isinstance(supplied, dict):
            raise SeatError("unauthorized", 401)
        values = {str(name).lower(): [str(value)] for name, value in supplied.items()}

    def one(name: str, *, required: bool = True) -> str:
        items = values.get(name, [])
        if len(items) > 1 or (not items and required) or (required and not items[0]):
            raise SeatError("unauthorized", 401)
        return items[0] if items else ""

    core_token = one("x-ax-gate-token")
    device_id = one("x-ax-device-id")
    device = one("x-ax-device")
    request_id = one("x-ax-request-id")
    if any("\r" in value or "\n" in value for value in (core_token, device_id, device, request_id)):
        raise SeatError("unauthorized", 401)
    enroll = isinstance(scope, dict) and scope.get("path") == "/v1/seat/enroll"
    authorization = one("authorization", required=not enroll)
    if authorization:
        if not authorization.startswith("Bearer ") or not authorization[7:] or "\r" in authorization or "\n" in authorization:
            raise SeatError("unauthorized", 401)
        bearer = authorization[7:]
    else:
        bearer = ""
    return RelayEvidence(core_token, device_id, device, bearer)


class SeatService:
    def __init__(self, store: SeatStore, *, seat_uid: Callable[[], int | None],
                 gate_address: str, gate_token: Callable[[], str], defaults,
                 emit: Callable[[str, str, dict], None] | None = None,
                 effects: dict[str, Callable[[str, str], None]] | None = None,
                 relay_decoder: Callable[[dict], RelayEvidence] = decode_relay_evidence,
                 hq_verifier: Callable[[dict, dict], object] | None = None):
        self.store, self.seat_uid = store, seat_uid
        self.gate_address, self.gate_token, self.defaults = gate_address, gate_token, defaults
        self.emit_hook = emit or (lambda *args: None)
        self.effects, self.relay_decoder, self.hq_verifier = effects or {}, relay_decoder, hq_verifier
        self._audit_last = {}
        self._writes = defaultdict(deque)
        self._rejects = defaultdict(int)
        self._sessions = defaultdict(int)
        self._lock = threading.RLock()
        register_via_factory("ssh", self.ssh_transport_ident)

    def emit(self, kind: str, principal: str, data: dict | None = None):
        try:
            self.emit_hook(kind, principal, data or {})
        except Exception:
            pass

    def failed(self, via: str, reason: str):
        now = timeutil.now().timestamp()
        key = (via, reason)
        with self._lock:
            previous = self._audit_last.get(key)
            if previous is None or now - previous >= float(self.defaults.get("audit_window_s")):
                self._audit_last[key] = now
                self.emit("auth.failed", via, {"via": via, "reason": reason})

    def ssh_transport_ident(self, uid: int) -> Ident | None:
        allowed = self.seat_uid()
        if allowed is None or uid != allowed:
            return None
        return Ident("ssh", "commander", "", "read")

    def authenticate_connection(self, via: str, conn) -> Ident | None:
        try:
            uid = peercred.peer_uid(conn)
            if via == "local":
                ident = local_ident(uid)
            elif via == "hq":
                ident = hq_ident(uid)
            elif via == "ssh":
                ident = self.ssh_transport_ident(uid)
            else:
                ident = None
        except Exception:
            ident = None
        if ident is None:
            self.failed(via, "hq_peer_uid_mismatch" if via == "hq" else "uid_mismatch")
        return ident

    def _locked(self, via: str):
        if via not in {"ssh", "tailnet"}:
            return
        if self.store.policy("lock:" + via, False):
            raise SeatError("locked", 503)

    def authorize_row(self, row: dict, *, read: bool = False) -> Ident:
        self._locked(row["via"])
        if row["state"] == "revoked":
            raise SeatError("seat_revoked", 401)
        if timeutil.parse_ts(row["expires_at"]) <= timeutil.now():
            self.expired(row)
            raise SeatError("seat_expired", 401)
        if row["state"] == "suspended" and not (read and self.store.policy("auto_suspended:" + row["seat_id"], False)):
            raise SeatError("seat_suspended", 403)
        tier = "read" if row["state"] == "suspended" or self.store.policy("lockdown", False) else row["tier"]
        return Ident(row["via"], "commander", row["seat_id"], tier)

    def ssh(self, transport: Ident, seat_id: str, *, read: bool = False) -> Ident:
        if transport.via != "ssh" or transport.principal:
            raise SeatError("forged_authority")
        row = self.store.get(seat_id)
        if row["via"] != "ssh":
            raise SeatError("seat_unknown", 401)
        return self.authorize_row(row, read=read)

    def relay(self, peer_address: str, evidence: RelayEvidence, *, enroll=False, read=False) -> Ident | None:
        # 固定アドレスと core token を照合してから端末の欄を扱う。
        try:
            expected = self.gate_token()
        except Exception:
            expected = ""
        if not self.gate_address or peer_address != self.gate_address or not expected or not secrets.compare_digest(expected, evidence.core_token):
            self.failed("tailnet", "gate_credential_mismatch")
            raise SeatError("unauthorized", 401)
        if not evidence.device_id or not evidence.device:
            raise SeatError("peer_unknown")
        self._locked("tailnet")
        if enroll:
            return None
        row = self.store.by_token(evidence.bearer, evidence.device_id)
        return self.authorize_row(row, read=read)

    def local_tier(self, ident: Ident) -> Ident:
        if ident.via == "local":
            # 席の台帳が使えなくても local を止めない。lockdown は SQLite の値だけを使う。
            try:
                lockdown = self.store.policy("lockdown", False)
            except (SeatError, sqlite3.Error):
                lockdown = False
            return Ident("local", "commander", local_principal(), "read" if lockdown else "operate")
        return ident

    def assert_body(self, ident: Ident, body: dict, declared: dict, *, scope: dict | None = None):
        expected = {"from": ident.from_, "principal": ident.principal, "via": ident.via,
                    "issued_by": "user" if ident.via == "hq" else "commander"}
        if any(key in body and body[key] != value for key, value in expected.items()):
            raise SeatError("forged_authority")
        if ident.via != "hq" and (body.get("confirm_irreversible") is True or "approved_via" in body):
            raise SeatError("forged_authority")
        if "tier" in body or "via" in body:
            raise SeatError("forged_authority")
        if any(key in body and body[key] != declared.get(key) for key in ("agent", "model")):
            raise SeatError("forged_authority")
        from axcommon.hq_proof import requires_proof
        if ident.via == "hq" and requires_proof(body.get("destination")):
            if self.hq_verifier is None:
                raise NotImplementedError("S-6.8/S-6.10: HQ identity evidence verifier must be supplied by HQ/approval")
            from core.intake.approval.store import ApprovalError
            try:
                proof = self.hq_verifier(scope or {}, body)
            except ApprovalError as error:
                raise SeatError(error.reason, error.status) from None
            if not (getattr(proof, "tailnet_allowed", False) and getattr(proof, "direct_tailnet", False)):
                raise SeatError("unauthorized")
            needs_factor = body.get("destination") in {"approval.grant", "seat.add", "seat.enroll_start", "seat.tier", "seat.unlock"}
            if needs_factor and not getattr(proof, "second_factor", False):
                raise SeatError("bad_code")
        elif ident.via in {"ssh", "tailnet"} and not body.get("idempotency_key"):
            raise SeatError("idempotency_required", 400)

    def check_available(self, ident: Ident, body: dict):
        if ident.via not in {"ssh", "tailnet"} or not self.store.degraded:
            return
        destination, payload = body.get("destination"), body.get("payload", {})
        stopping = destination in {"seat.revoke", "work.stop", "work.cancel", "change.cancel_merge"}
        stopping |= destination == "control.command" and payload.get("op") == "cancel"
        if not stopping:
            raise SeatError("unavailable", 503)

    def drain_effects(self):
        for effect in self.store.pending_effects():
            action, principal, reason = effect["action"], effect["principal"], effect["reason"]
            callback = self.effects.get(action)
            if callback is None:
                self.store._alert("seat_effect_pending:" + action)
                continue
            try:
                callback(principal, reason)
            except Exception:
                self.store._alert("seat_effect_failed:" + action)
                continue
            self.store.finish_effect(principal, reason, action)

    def revoke(self, seat_id: str, actor: Ident):
        if actor.via != "hq" and (actor.via not in {"ssh", "tailnet"} or actor.principal != seat_id):
            raise SeatError("forged_authority")
        row = self.store.get(seat_id)
        actions = CLEANUP + (("seat_sync",) if row["via"] == "ssh" else ())
        row = self.store.transition(seat_id, state="revoked", actor=actor.principal,
                                    reason="requester_revoked", actions=actions)
        self.emit("seat.revoked", seat_id)
        self.drain_effects()
        return row

    def expired(self, row: dict):
        with self.store.lock:
            name = "expired:" + row["seat_id"]
            if self.store.policy(name, False):
                return
            actions = CLEANUP + (("seat_sync",) if row["via"] == "ssh" else ())
            self.store.transition(row["seat_id"], state=row["state"], actor="intake",
                                  reason="requester_expired", actions=actions)
            self.store.set_policy(name, True)
        self.emit("seat.expired", row["seat_id"])
        self.drain_effects()

    def set_lock(self, actor: Ident, scope: str, value: bool, *, approved: bool = False):
        if actor.via != "hq" or (not value and not approved):
            raise SeatError("forged_authority")
        if scope not in {"ssh", "tailnet", "all"}:
            raise ValueError("invalid lock scope")
        for via in (("ssh", "tailnet") if scope == "all" else (scope,)):
            self.store.set_policy("lock:" + via, value)
        if value and scope in {"ssh", "all"}:
            with self.store.lock, self.store._database():
                self.store.db.execute("INSERT OR IGNORE INTO seat_effects VALUES(?,?,?,0)", ("*ssh*", "locked", "seat_sync"))
            self.drain_effects()

    def lockdown(self, actor: Ident, value: bool, *, approved: bool = False):
        if actor.via != "hq" or (not value and not approved):
            raise SeatError("forged_authority")
        self.store.set_policy("lockdown", value)
        self.set_lock(actor, "all", value, approved=approved)
        if value:
            principals = list((self.store.rows or {}).keys()) + [local_principal()]
            with self.store.lock, self.store._database():
                self.store.db.executemany("INSERT OR IGNORE INTO seat_effects VALUES(?,?,?,0)",
                                         [(p, "lockdown", a) for p in principals for a in CLEANUP])
            self.drain_effects()
        self.emit("commander.lockdown.on" if value else "commander.lockdown.off", "commanders")

    def count_instruction(self, ident: Ident):
        if ident.via not in {"ssh", "tailnet"}:
            return
        with self._lock:
            now = timeutil.now().timestamp()
            writes = self._writes[ident.principal]
            cutoff = now - float(self.defaults.get("commander_rate_window_min")) * 60
            while writes and writes[0] <= cutoff:
                writes.popleft()
            writes.append(now)
            if len(writes) > int(self.defaults.get("commander_rate_max")):
                self.suspend(ident.principal)
                raise SeatError("seat_suspended")

    def open_session(self, ident: Ident) -> None:
        if ident.via != "ssh":
            return
        with self._lock:
            count = self._sessions[ident.principal]
            if count >= int(self.defaults.get("seat_max_sessions")):
                raise SeatError("seat_sessions_exceeded", 429)
            self._sessions[ident.principal] = count + 1

    def close_session(self, ident: Ident) -> None:
        if ident.via != "ssh":
            return
        with self._lock:
            self._sessions[ident.principal] = max(0, self._sessions[ident.principal] - 1)

    def count_result(self, ident: Ident, rejected: bool):
        if ident.via not in {"ssh", "tailnet"}:
            return
        with self._lock:
            self._rejects[ident.principal] = self._rejects[ident.principal] + 1 if rejected else 0
            if self._rejects[ident.principal] >= int(self.defaults.get("commander_reject_suspend")):
                self.suspend(ident.principal)

    def suspend(self, seat_id: str):
        self.store.set_policy("auto_suspended:" + seat_id, True)
        self.store.transition(seat_id, state="suspended", actor="intake", reason="rate_suspended", actions=("notify_suspended",))
        self.emit("seat.suspended", seat_id)
        self.drain_effects()

    def start_enrollment(self, draft: dict, approval_id: str, *, bound_device_id: str | None = None) -> str:
        """Create a one-use code; the first verified gate exchange binds its device."""
        if not approval_id or set(draft) != {"label", "tier"} or draft["tier"] not in {"read", "operate", "irreversible"} or not draft["label"]:
            raise ValueError("invalid enrollment draft")
        if bound_device_id is not None and (not isinstance(bound_device_id, str) or not bound_device_id):
            raise ValueError("invalid enrollment device")
        code = secrets.token_hex(16)
        expires = timeutil.add_seconds(timeutil.now_ts(), float(self.defaults.get("gate_enroll_code_min")) * 60)
        stored = dict(draft, created_by=approval_id)
        with self.store.lock, self.store._database():
            self.store.db.execute("INSERT INTO enroll_codes(code_digest,seat_draft_json,device_id,expires_at) VALUES(?,?,?,?)",
                                 (digest(code), json.dumps(stored), bound_device_id, expires))
        return code

    def enroll(self, code: str, evidence: RelayEvidence) -> dict:
        self._locked("tailnet")
        now = timeutil.now_ts()
        with self.store.lock:
            db = self.store._database()
            failure = db.execute("SELECT * FROM seat_enroll_failures WHERE device_id=?", (evidence.device_id,)).fetchone()
            if failure and failure["locked_until"] and timeutil.parse_ts(failure["locked_until"]) > timeutil.now():
                raise SeatError("locked", 503)
            row = db.execute("SELECT * FROM enroll_codes WHERE code_digest=?", (digest(code),)).fetchone()
            if row is None or row["used_at"] is not None or timeutil.parse_ts(row["expires_at"]) <= timeutil.now() or (row["device_id"] is not None and row["device_id"] != evidence.device_id):
                count = (failure["fail_count"] if failure and not failure["locked_until"] else 0) + 1
                until = timeutil.add_seconds(now, float(self.defaults.get("gate_enroll_lock_min")) * 60) if count >= int(self.defaults.get("gate_enroll_fail_max")) else None
                with db:
                    db.execute("INSERT INTO seat_enroll_failures VALUES(?,?,?) ON CONFLICT(device_id) DO UPDATE SET fail_count=excluded.fail_count,locked_until=excluded.locked_until", (evidence.device_id, count, until))
                    if row:
                        db.execute("UPDATE enroll_codes SET fail_count=fail_count+1 WHERE code_digest=?", (digest(code),))
                raise SeatError("unauthorized", 401)
            draft = json.loads(row["seat_draft_json"])
            token = secrets.token_hex(32)
            seat_id = "seat-" + ids.new_ulid().lower()
            seat = dict(draft, seat_id=seat_id, via="tailnet", device=evidence.device,
                        device_id=evidence.device_id, token_digest=digest(token), state="active", created_at=now,
                        expires_at=timeutil.add_seconds(now, float(self.defaults.get("seat_default_ttl_days")) * 86400))
            # 使用済みを先に永続化する。後の保存失敗でも同じコードを二重発行しない。
            with db:
                db.execute("UPDATE enroll_codes SET used_at=? WHERE code_digest=? AND used_at IS NULL", (now, digest(code)))
            self.store.put(seat)
            with db:
                db.execute("INSERT INTO seat_enroll_failures VALUES(?,0,NULL) ON CONFLICT(device_id) DO UPDATE SET fail_count=0,locked_until=NULL", (evidence.device_id,))
        self.emit("seat.added", seat_id, {"via": "tailnet"})
        return {"seat_id": seat_id, "token": token, "expires_at": seat["expires_at"]}

    def add_ssh(self, draft: dict, approval_id: str) -> dict:
        """Create an SSH seat from the HQ-only ``seat.add`` payload."""
        expected = {"label", "device", "device_id", "public_key", "tier"}
        if not isinstance(draft, dict) or set(draft) != expected or not approval_id:
            raise ValueError("invalid SSH seat draft")
        if draft["tier"] not in {"read", "operate", "irreversible"}:
            raise ValueError("invalid SSH seat tier")
        if any(not isinstance(draft[name], str) or not draft[name] for name in ("label", "device", "device_id", "public_key")):
            raise ValueError("invalid SSH seat draft")
        public_key = draft["public_key"].strip()
        seat_id = "seat-" + ids.new_ulid().lower()
        now = timeutil.now_ts()
        row = {
            "seat_id": seat_id,
            "label": draft["label"],
            "via": "ssh",
            "device": draft["device"],
            "device_id": draft["device_id"],
            "public_key": public_key,
            "key_fp": key_fingerprint(public_key),
            "tier": draft["tier"],
            "state": "active",
            "created_at": now,
            "created_by": approval_id,
            "expires_at": timeutil.add_seconds(now, float(self.defaults.get("seat_default_ttl_days")) * 86400),
        }
        row = self.store.put(row)
        with self.store.lock, self.store._database():
            self.store.db.execute("INSERT OR IGNORE INTO seat_effects VALUES(?,?,?,0)", (seat_id, "seat_added", "seat_sync"))
        self.emit("seat.added", seat_id, {"via": "ssh"})
        self.drain_effects()
        return row

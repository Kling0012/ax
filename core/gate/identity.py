from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SEAT_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,31}\Z")


def decode_rows(raw: bytes) -> dict[str, dict]:
    """gate に必要な読み取り専用の席の最小検査。壊れた台帳は採用しない。"""
    import yaml
    doc = yaml.safe_load(raw)
    if not isinstance(doc, dict) or set(doc) != {"seats"} or not isinstance(doc["seats"], list):
        raise ValueError("invalid seats document")
    rows = {}
    for row in doc["seats"]:
        required = {"seat_id", "via", "device", "device_id", "token_digest", "tier", "state", "expires_at"}
        if not isinstance(row, dict) or not required <= set(row) or row.get("via") != "tailnet":
            raise ValueError("invalid seat")
        if not SEAT_ID.fullmatch(row["seat_id"]) or row["seat_id"] in rows:
            raise ValueError("invalid seat id")
        if row["tier"] not in {"read", "operate", "irreversible"} or row["state"] not in {"active", "suspended", "revoked"}:
            raise ValueError("invalid state")
        if any(not isinstance(row[key], str) or not row[key] for key in required):
            raise ValueError("invalid seat value")
        rows[row["seat_id"]] = row
    return rows


class Denied(Exception):
    def __init__(self, code, status=403):
        super().__init__(code)
        self.code, self.status = code, status


class Identities:
    """Peers fail closed; seats retain the last parseable view (intake is authoritative)."""
    def __init__(self, settings, *, now=None, alert=None):
        self.settings = settings
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.alert = alert or (lambda reason: None)
        self.rows = {}

    def peer(self, source_ip):
        try:
            ip = ipaddress.ip_address(source_ip)
            if ip.version != 4 or ip not in ipaddress.ip_network("100.64.0.0/10"):
                raise ValueError("not tailnet")
            if str(ip) == self.settings.bind_addr:
                raise ValueError("self")
            peers = json.loads((self.settings.state_dir / "peers.json").read_bytes())
            allowed = set(self.settings.allowed_users_file.read_text().splitlines()) - {""}
            peer = peers[str(ip)]
            if (not isinstance(peer, dict) or peer["user_login"] not in allowed
                    or not isinstance(peer["device_id"], str) or not peer["device_id"]):
                raise ValueError("not allowed")
            return peer
        except (OSError, ValueError, TypeError, KeyError):
            raise Denied("peer_unknown") from None

    def seat(self, authorization, peer):
        if not authorization.startswith("Bearer ") or not authorization[7:]:
            raise Denied("unauthorized", 401)
        token_digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        try:
            self.rows = decode_rows(self.settings.seats_file.read_bytes())
        except Exception:
            self.alert("seats_unreadable")
        for row in self.rows.values():
            if row["via"] != "tailnet" or not hmac.compare_digest(row["token_digest"], token_digest):
                continue
            if row["device_id"] != peer["device_id"]:
                raise Denied("unauthorized", 401)
            if row["state"] != "active":
                raise Denied("seat_" + row["state"], 401)
            expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if expiry <= self.now():
                raise Denied("seat_expired", 401)
            if not SEAT_ID.fullmatch(row["seat_id"]):
                raise Denied("unauthorized", 401)
            try:
                revoked = self.settings.state_dir / "revoked.d"
                # A missing or unreadable revocation directory is not proof of validity.
                with __import__("os").scandir(revoked) as entries:
                    if any(entry.name == row["seat_id"] for entry in entries):
                        raise Denied("seat_revoked", 401)
            except OSError:
                raise Denied("unauthorized", 401) from None
            return row
        raise Denied("unauthorized", 401)

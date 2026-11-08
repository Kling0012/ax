"""Offline HQ, command, ledger and schema adapters. No production credentials."""
from __future__ import annotations

from copy import deepcopy
import hashlib

from core.intake.approval import VerifiedUser
from core.intake.vias import Ident

HQ = Ident("hq", "hq", "user", "irreversible", "uid=2001")


def seat(via="ssh", tier="irreversible", principal="seat-a"):
    return Ident(via, "commander", principal, tier)


def instruction(name="core.change", payload=None, key="request-1"):
    return {"destination": name, "payload": deepcopy(payload if payload is not None else {"reason": "変更の依頼"}),
            "idempotency_key": key, "session": "declared-session", "project": "demo"}


def destination(name="core.change", tier="irreversible", via=None):
    return {"name": name, "title": "検証用", "tier": tier, "irreversible": tier == "irreversible",
            "via": via or ["local", "ssh", "tailnet", "hq"], "allowed_from": ["commander", "hq"],
            "schema": "schemas/dest/" + name + ".json", "handler": "http://fake:8080/v1/act", "ack": "sync"}


def schema_for(name):
    if name.endswith("go.confirm.json"):
        return go_schema()
    props = {k: {} for k in ("reason", "todo_id", "quote", "answer", "answer_source", "target", "op", "args",
                             "spec", "seat_id", "approval_id", "digest", "evidence")}
    props["args"] = {"type": "object", "properties": {"mode": {"type": "string"}}, "additionalProperties": False}
    return {"type": "object", "properties": props, "additionalProperties": False}


def go_schema():
    # Fake b12b2-2 boundary, exactly the S-6.22 input variants.
    changes = {"oneOf": [{"const": "all"}, {"type": "array", "uniqueItems": True,
                "items": {"type": "integer", "minimum": 1, "maximum": 14}}]}
    props = {"kind": {"enum": ["removal", "ratify"]}, "reason": {"type": "string", "maxLength": 500},
             "scope": {"enum": ["removal", "both"]},
             "excluded": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 200}},
             "changes": changes, "ratify_approval_id": {"type": ["string", "null"]},
             "go_md_text": {"type": "string"}, "go_md_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}
    return {"type": "object", "oneOf": [
        {"properties": {**{k: props[k] for k in ("kind", "reason", "scope", "excluded", "changes", "ratify_approval_id")},
                        "kind": {"const": "removal"}},
         "required": ["kind", "scope", "excluded", "changes", "ratify_approval_id"], "additionalProperties": False},
        {"properties": {**{k: props[k] for k in ("kind", "reason", "go_md_text", "go_md_sha256")}, "kind": {"const": "ratify"}},
         "required": ["kind", "go_md_text", "go_md_sha256"], "additionalProperties": False}]}


def go_text(scope="構築", changes="全部", quote="検証用の架空の発言"):
    return ("# GO\n受けた時刻（UTC）: 2026-11-05T12:00:00Z\n受けたセッション: fake\n"
            f"原文: {quote}\n範囲: {scope}\n範囲外と明示された物: なし\n承認に含めた変更: {changes}\n")


def ratify(text=None):
    text = go_text() if text is None else text
    return {"kind": "ratify", "go_md_text": text, "go_md_sha256": hashlib.sha256(text.encode()).hexdigest()}


def removal(ratify_id=None):
    return {"kind": "removal", "scope": "both", "excluded": [], "changes": "all", "ratify_approval_id": ratify_id}


class FakeHQ:
    """Factor seed/code handling deliberately stays on this side of the boundary."""
    def __init__(self):
        self.valid = True
        self.direct = True
        self.second = True
        self.used = set()
        self.locked = False
        self.failures = 0

    def factor_check(self, code, *, now, expires_at, fail_max=3):
        # Simulated code never crosses the intake boundary.
        self.second = False
        if self.locked:
            return False
        if code in self.used or now >= expires_at or code != "fake-factor":
            self.failures += 1
            self.locked = self.failures >= fail_max
            return False
        self.used.add(code)
        self.failures = 0
        self.second = True
        return True

    def verify(self, scope, body):
        return VerifiedUser(self.valid and not self.locked, self.second and not self.locked, self.direct)


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, command):
        self.calls.append(deepcopy(command))
        if self.fail:
            raise RuntimeError("simulated uncertain response")
        return {"ok": True}


class FakeSeatState:
    def __init__(self):
        self.revoked = set()
        self.expired = set()
        self.locked = set()
        self.lockdown = False

    def __call__(self, ident):
        if ident.principal in self.revoked:
            return "seat_revoked"
        if ident.principal in self.expired:
            return "seat_expired"
        if self.lockdown and ident.via != "hq":
            return "lockdown"
        if ident.via in self.locked:
            return "locked"
        return None

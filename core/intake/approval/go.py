"""D-5.5 go functions, kept inside the assigned approval package."""
from __future__ import annotations

import hashlib
import json
import re

from axcommon import schemas
from axcommon.redact import Redactor
from .store import ApprovalError, canonical_json, payload_digest

GO_KEYS = ("受けた時刻（UTC）", "受けたセッション", "原文", "範囲", "範囲外と明示された物", "承認に含めた変更")
SCOPES = {"構築": "build", "撤去": "removal", "両方": "both", "build": "build", "removal": "removal", "both": "both"}
EXPORT_KEYS = ("承認 ID", "種別", "digest", "承認した時刻（UTC）", "依頼した席", "範囲",
               "範囲外と明示された物", "承認に含めた変更", "追認の承認 ID", "payload")
EXPORT_TITLE = "# GO（承認の記録から書き出した物。手で直さない）"


def parse_go_md(text: str) -> dict:
    def invalid():
        raise ApprovalError(422, "go_md_unparsable")
    if not isinstance(text, str) or "\r" in text or "\x00" in text:
        invalid()
    lines = text.split("\n")
    if not lines or lines[0] != "# GO":
        invalid()
    values, current = {}, None
    for line in lines[1:]:
        key, sep, value = line.partition(": ")
        if sep and key in GO_KEYS:
            if key in values:
                invalid()
            current = key
            values[key] = value
        elif current is None:
            if line.strip():
                invalid()
        else:
            values[current] += "\n" + line
    values = {k: v.strip() for k, v in values.items()}
    if set(values) != set(GO_KEYS) or any(not v for v in values.values()) or values["範囲"] not in SCOPES:
        invalid()
    raw = values["承認に含めた変更"]
    if raw == "全部":
        changes = "all"
    elif raw == "なし":
        changes = []
    elif re.fullmatch(r"[0-9]+(?:\s*,\s*[0-9]+)*", raw):
        changes = [int(s.strip()) for s in raw.split(",")]
        if len(set(changes)) != len(changes) or any(n < 1 or n > 14 for n in changes):
            invalid()
    else:
        invalid()
    excluded = values["範囲外と明示された物"]
    return {"scope": SCOPES[values["範囲"]], "excluded": [] if excluded == "なし" else [excluded], "changes": changes}


def _record_ok(row: dict) -> bool:
    result = json.loads(row["result_json"])
    return (row["destination"] == "go.confirm" and row["state"] == "executed"
            and bool(row["granted_at"]) and bool(row["executed_at"])
            and result.get("granted_by") == "user"
            and payload_digest(json.loads(row["payload_json"])) == row["digest"]
            and isinstance(result.get("go"), dict))


class GoConfirm:
    def __init__(self, store, *, schema_loader=schemas.load, redactor=None, max_bytes=8192):
        self.store, self.schema_loader = store, schema_loader
        self.redactor = redactor or Redactor()
        self.max_bytes = max_bytes

    def validate(self, payload: dict) -> dict:
        import jsonschema
        try:
            schema = self.schema_loader("dest/go.confirm.json")
        except (FileNotFoundError, KeyError):
            raise NotImplementedError("D-5.5/S-6.22: schemas/dest/go.confirm.json is owned by b12b2-2") from None
        validator = schemas.validator("dest/go.confirm.json") if self.schema_loader is schemas.load else jsonschema.Draft202012Validator(schema)
        if not validator.is_valid(payload):
            raise ApprovalError(422, "bad_request")
        derived = {}
        if payload["kind"] == "ratify":
            text = payload["go_md_text"]
            try:
                raw = text.encode("utf-8")
            except UnicodeEncodeError:
                raise ApprovalError(422, "bad_request") from None
            if len(raw) > self.max_bytes or "\r" in text:
                raise ApprovalError(422, "bad_request")
            if hashlib.sha256(raw).hexdigest() != payload["go_md_sha256"]:
                raise ApprovalError(422, "go_md_hash_mismatch")
            derived = parse_go_md(text)
        elif payload.get("ratify_approval_id") is not None:
            try:
                ratify = self.store.get(payload["ratify_approval_id"])
                result = json.loads(ratify["result_json"])
                if (not _record_ok(ratify) or result["go"]["kind"] != "ratify"
                        or result["go"]["scope"] not in {"build", "both"}):
                    raise ApprovalError(422, "ratify_not_effective")
            except (ApprovalError, KeyError):
                raise ApprovalError(422, "ratify_not_effective") from None
        for key in ("go_md_text", "reason", "excluded"):
            if key in payload and self.redactor.scan(payload[key] if isinstance(payload[key], str) else "\n".join(payload[key])):
                raise ApprovalError(422, "scan_blocked")
        return derived

    def execute(self, approval: dict) -> dict:
        payload = json.loads(approval["payload_json"])
        result = json.loads(approval["result_json"])
        derived = result.get("derived", {}) if payload["kind"] == "ratify" else payload
        return {"kind": payload["kind"], "scope": derived["scope"], "excluded": derived["excluded"],
                "changes": derived["changes"], "ratify_approval_id": payload.get("ratify_approval_id"),
                "go_md_sha256": payload.get("go_md_sha256"), "granted_at": approval["granted_at"]}

    def export_view(self, approval_id: str) -> bytes:
        row = self.store.get(approval_id)
        payload = json.loads(row["payload_json"])
        if not _record_ok(row) or payload["kind"] != "removal":
            raise ApprovalError(422, "verify_failed")
        scope = {"both": "両方", "removal": "撤去"}[payload["scope"]]
        excluded = "、".join(payload["excluded"]) or "なし"
        changes = payload["changes"]
        changes = "全部" if changes == "all" else ",".join(map(str, changes)) or "なし"
        values = [row["approval_id"], "removal", row["digest"], row["granted_at"], row["requested_by"],
                  scope, excluded, changes, payload["ratify_approval_id"] or "なし", row["payload_json"]]
        return (EXPORT_TITLE + "\n" + "\n".join(f"{k}: {v}" for k, v in zip(EXPORT_KEYS, values)) + "\n").encode("utf-8")

    def verify_view(self, approval_id: str, go_file_bytes: bytes | None = None,
                    build_go_bytes: bytes | None = None) -> dict:
        try:
            row = self.store.get(approval_id)
        except ApprovalError:
            return {"approval_id": approval_id, "kind": None, "state": None, "executed": False,
                    "digest": None, "requested_by": None, "scope": None, "excluded": [], "changes": [],
                    "ratify": None, "checks": {"record": "failed", "go_file": "skipped", "build_go": "skipped"}}
        payload = json.loads(row["payload_json"])
        result = json.loads(row["result_json"])
        go = result.get("go", {})
        ok = _record_ok(row)
        # A ratification alone is never a removal permission (T-194m).
        checks = {"record": "passed" if ok and go.get("kind") == "removal" else "failed",
                  "go_file": "skipped", "build_go": "skipped"}
        if go_file_bytes is not None:
            if payload.get("kind") == "ratify":
                matches = ok and hashlib.sha256(go_file_bytes).hexdigest() == payload.get("go_md_sha256")
            else:
                try:
                    # Export is canonical: rejecting any edit also enforces all semantic checks.
                    matches = ok and go_file_bytes == self.export_view(approval_id)
                except ApprovalError:
                    matches = False
            checks["go_file"] = "passed" if matches else "mismatch"
        ratify = None
        if build_go_bytes is not None:
            ratify_id = payload.get("ratify_approval_id")
            ratify_ok = False
            if payload.get("kind") == "removal" and ratify_id:
                try:
                    r = self.store.get(ratify_id)
                    rgo = json.loads(r["result_json"]).get("go", {})
                    ratify_ok = (_record_ok(r) and rgo.get("kind") == "ratify"
                                 and rgo.get("scope") in {"build", "both"}
                                 and hashlib.sha256(build_go_bytes).hexdigest() == rgo.get("go_md_sha256"))
                except ApprovalError:
                    pass
            checks["build_go"] = "passed" if ratify_ok else "failed"
            ratify = {"approval_id": ratify_id, "ok": ratify_ok}
        return {"approval_id": approval_id, "kind": payload.get("kind"), "state": row["state"],
                "executed": row["state"] == "executed", "digest": row["digest"], "requested_by": row["requested_by"],
                "scope": go.get("scope"), "excluded": go.get("excluded", []), "changes": go.get("changes", []),
                "ratify": ratify, "checks": checks}

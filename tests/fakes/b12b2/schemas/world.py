"""File-only worlds for schema consistency tests; no AX service is contacted."""
from __future__ import annotations

import json
from pathlib import Path

from tools.check_dest_schemas import DESTINATIONS


EXAMPLES = {
    "control.command": {"target": "demo", "op": "run_now", "args": {}, "reason": "check"},
    "work.request": {"project": "demo", "title": "check", "role": "research"},
    "ledger.answer": {"todo_id": "demo", "answer": "yes", "quote": "proceed"},
    "secret.push": {"slot": "demo-slot"},
    "emergency": {"scope": "commanders"},
    "change.request": {"issue_repo": "owner/demo", "kind": "issue", "number": 1, "dry_run": True},
    "change.cancel_merge": {"issue_repo": "owner/demo", "number": 1, "reason": "stop"},
    "ledger.complete": {"todo_id": "demo"},
    "ledger.park": {"todo_id": "demo", "reason": "wait", "check_at": "2026-11-08T00:00:00Z"},
    "decision.record": {"kind": "decision", "title": "demo", "body": "check", "basis": "commander"},
    "claim.op": {"op": "acquire", "target_key": "demo:one"},
    "seat.enroll_start": {"label": "demo", "tier": "read"},
    "seat.tier": {"seat_id": "demo-seat", "tier": "read"},
    "seat.revoke": {"seat_id": "demo-seat"},
    "seat.lock": {"scope": "ssh"},
    "seat.unlock": {"scope": "all"},
    "approval.grant": {"approval_id": "apr-demo", "digest": "a" * 64},
    "approval.deny": {"approval_id": "apr-demo"},
    "approval.withdraw": {"approval_id": "apr-demo"},
    "go.confirm": {"kind": "removal", "scope": "both", "excluded": [], "changes": [], "ratify_approval_id": None},
}


def row(name: str) -> dict:
    return {"name": name, "title": name, "schema": f"schemas/dest/{name}.json",
            "handler": "http://fake.invalid/unused", "allowed_from": ["commander", "hq"],
            "via": ["local", "ssh", "tailnet", "hq"], "tier": "operate", "irreversible": False}


def make_world(tmp_path: Path, source: Path, *, omit_schema: str | None = None,
               omit_row: str | None = None) -> Path:
    """Build a complete *synthetic* registry, not a production readiness claim.

    Unresolved real contracts are replaced by an empty closed object here only,
    to isolate correspondence checks from the reported design gaps.
    """
    root = tmp_path / "world"
    (root / "schemas/dest").mkdir(parents=True)
    (root / "config/intake-destinations.d").mkdir(parents=True)
    for name in ("common.schema.json", "intake-destinations.schema.json"):
        (root / "schemas" / name).write_bytes((source / "schemas" / name).read_bytes())
    for name in sorted(DESTINATIONS):
        if name == omit_schema:
            continue
        doc = json.loads((source / "schemas/dest" / f"{name}.json").read_text())
        if doc.pop("x-ax-unresolved", None):
            doc.pop("not")
        (root / "schemas/dest" / f"{name}.json").write_text(json.dumps(doc))
    rows = [row(n) for n in sorted(DESTINATIONS) if n != omit_row]
    (root / "config/intake-destinations.yaml").write_text(json.dumps(rows))
    return root

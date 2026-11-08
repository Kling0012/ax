"""Core changes execute only against the stored, personally granted payload."""
from __future__ import annotations

from axcommon import schemas
from core.intake.dest.b12 import common, mappers


def register(app):
    app.state.intake.dest_handlers["core.change"] = handle


def handle(app, dest, body, ident):
    # 'confirm_irreversible' or 'approved_via' in an incoming instruction is
    # never proof. B12b2 must call execute_approved from its trusted executor.
    return 403, {"error": {"code": "approval_required", "message": "approval_required"}}


def execute_approved(app, command):
    e = common.engine(app)
    if command.get("issued_by") != "user" or command.get("destination") != "core.change":
        raise common.fault(403, "approval_required")
    approval_id = command.get("approved_via")
    e.approved(approval_id, payload=command.get("payload"))
    payload = command["payload"]
    schemas.validate("dest/core.change.json", payload)
    item = mappers.core_change_item(payload, "core-" + approval_id)
    return common.submit_item(app, item, approval_id=approval_id)

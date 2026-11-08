"""Wire decoding for the B12 subloop request."""
from __future__ import annotations

from copy import deepcopy
import re
import subprocess

from core.intake.dest.b12.subloop import Request


def decode(payload: dict) -> Request:
    """Convert a schema-validated payload without changing its declarations."""
    return Request(
        plugin_id=payload["plugin_id"],
        project=payload["project"],
        title=payload["title"],
        body=payload["body"],
        spec=deepcopy(payload["spec"]),
        acceptance=deepcopy(payload["acceptance"]),
        quote=payload.get("quote"),
    )


def handle(app, dest, body, ident):
    """Validate the destination payload before invoking the creation service."""
    from axcommon import schemas

    payload = body.get("payload")
    errors = schemas.errors("dest/subloop.create.json", payload)
    if errors:
        return 422, {"error": {"code": "payload_schema", "message": "; ".join(errors[:3])}}
    service = getattr(app.state, "b12_subloop", None)
    if service is None:
        return 503, {"error": {"code": "subloop_unavailable"}}
    return service.create(body.get("idempotency_key"), decode(payload), ident)


def git_head_reader(staging):
    """Read only an exact candidate ref from the trusted staging repository."""
    def read_head(repository, branch):
        if (repository != 'work.git' or not isinstance(branch, str)
                or not re.fullmatch(r'subloop/[a-z0-9][a-z0-9_/-]*', branch)
                or '..' in branch or '//' in branch):
            raise ValueError('invalid candidate')
        ref = 'refs/heads/' + branch
        result = subprocess.run(['git', 'ls-remote', '--exit-code', '--refs', '--', str(staging), ref],
                                check=True, capture_output=True, text=True, timeout=30)
        rows = result.stdout.splitlines()
        if len(rows) != 1 or '\t' not in rows[0]:
            raise ValueError('candidate head unavailable')
        sha, actual_ref = rows[0].split('\t', 1)
        if actual_ref != ref or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha):
            raise ValueError('candidate head unavailable')
        return sha
    return read_head


def register(app, service):
    """Install the handler with an explicitly configured creation service."""
    engine = getattr(app.state, 'b12', None)
    backend = getattr(engine, 'backend', None)
    if backend is not None and getattr(backend, 'staging', None):
        service.ports.read_head = git_head_reader(backend.staging)
    app.state.b12_subloop = service
    app.state.intake.dest_handlers["subloop.create"] = handle


def execute_approved(app, command):
    """Replay only the payload and identity stored by the approval service."""
    import json
    from core.intake.approval.store import ApprovalError, payload_digest
    from core.intake.dest.b12.subloop.policy import Rejected
    from core.intake.vias import Ident

    approvals = app.state.approvals
    row = approvals.store.get(command.get("approved_via"))
    if (command.get("issued_by") != "user" or command.get("destination") != "subloop.create"
            or not row or row["destination"] != "subloop.create" or row["state"] != "granted"
            or not row.get("granted_at")):
        raise ApprovalError(403, "approval_required")
    payload = json.loads(row["payload_json"])
    saved = json.loads(row["result_json"])
    if payload_digest(payload) != row["digest"] or payload != command.get("payload"):
        raise ApprovalError(403, "approval_payload_mismatch")
    caller = saved["caller"]
    requester = Ident(caller["via"], "hq" if caller["via"] == "hq" else "commander",
                      row["requested_by"], caller["tier"])
    from axcommon import schemas
    schemas.validate("dest/subloop.create.json", payload)
    try:
        return app.state.b12_subloop._create(saved["request_key"], decode(payload), requester,
                                             approved_id=row["approval_id"])
    except Rejected as exc:
        return exc.status, {"reason": exc.reason}

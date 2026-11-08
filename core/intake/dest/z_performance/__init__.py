"""Connect authenticated control commands to the performance resume gate.

AX_PERFORMANCE_TARGETS maps registered control plugin IDs to performance targets.
AX_PERFORMANCE_DB is the existing database shared with the performance process.
Neither setting can be supplied by an instruction.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

from axcommon import schemas, timeutil
from axcommon.client import ApiClient
from axcommon.creds import read_secret_optional
from core.intake.dest.b5 import CONTROL_FIELDS
from urllib.parse import urlsplit


def _configuration():
    raw = os.environ.get("AX_PERFORMANCE_TARGETS")
    path = os.environ.get("AX_PERFORMANCE_DB")
    if not raw and not path:
        logging.getLogger(__name__).info("performance resume disabled: configuration absent")
        return None
    mapping = json.loads(raw or "null")
    if (not isinstance(mapping, dict) or not mapping or not path or not Path(path).is_absolute()
            or any(not isinstance(k, str) or not k.strip() or k == "all"
                   or not isinstance(v, str) or not v.strip() or v == "all"
                   for k, v in mapping.items())
            or len(set(mapping.values())) != len(mapping)):
        raise ValueError("invalid performance resume configuration")
    return mapping, path


def _accepted(result, target):
    if not isinstance(result, dict):
        return False
    if (not isinstance(result.get("id"), str) or not result["id"]
            or result.get("target") != target or result.get("op") != "set_mode"
            or result.get("issued_by") != "user" or result.get("state") != "active"
            or not isinstance(result.get("args"), dict) or result["args"].get("mode") != "run"):
        return False
    expiry = result.get("expires_at")
    if expiry:
        try:
            return timeutil.parse_ts(expiry) > timeutil.now()
        except (TypeError, ValueError):
            return False
    return True


def _resume(path, target):
    # Do not create a missing DB or recover a different database silently.
    uri = Path(path).as_uri() + "?mode=rw"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        db.execute("INSERT INTO targets (target, resumed) VALUES (?, 1) "
                   "ON CONFLICT(target) DO UPDATE SET resumed=1", (target,))


def register(app):
    config = _configuration()
    if config is None:
        return
    mapping, path = config
    previous = app.state.intake.dest_handlers.get("control.command")
    if previous is None:
        raise RuntimeError("control command handler unavailable")

    def control_command(app, dest, body, ident):
        payload = body.get("payload") or {}
        target = payload.get("target")
        if (target not in mapping or payload.get("op") != "set_mode"
                or (payload.get("args") or {}).get("mode") != "run"):
            return previous(app, dest, body, ident)
        # Only the identity established by the HQ listener counts. A commander
        # or a claimed issuer in the request cannot activate the gate.
        if ident.via != "hq" or ident.from_ != "hq" or ident.principal != "user":
            return 403, {"error": {"code": "performance_user_required"}}
        command = {k: payload[k] for k in CONTROL_FIELDS if k in payload}
        command["issued_by"] = "user"
        if schemas.errors("control-command-request", command):
            return 422, {"error": {"code": "payload_schema"}}
        token = read_secret_optional("core-token-intake")
        if not token:
            return 503, {"error": {"code": "performance_control_unavailable"}}
        u = urlsplit(dest.get("handler", ""))
        if u.scheme not in ("http", "https") or not u.netloc:
            return 503, {"error": {"code": "performance_control_unavailable"}}
        client = ApiClient(f"{u.scheme}://{u.netloc}", token=token, transport=app.state.intake.transport)
        result = client.post(u.path or "/", command)
        if not _accepted(result, target):
            return 503, {"error": {"code": "performance_command_unconfirmed"}}
        try:
            _resume(path, mapping[target])
        except (OSError, sqlite3.Error, ValueError):
            return 503, {"error": {"code": "performance_resume_unavailable"}}
        return 200, result

    app.state.intake.dest_handlers["control.command"] = control_command

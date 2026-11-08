"""Authenticated intake -> real control API -> persistent performance gate."""
import json
import sqlite3
import sys
from itertools import count
from urllib.parse import urlsplit
from pathlib import Path

import yaml
from starlette.testclient import TestClient

from core.intake.app import build_app
from core.intake.vias import hq_ident, local_ident
from plugins.subloop.performance import Performance


class Backend:
    def __init__(self):
        self.calls = []

    def prepare(self, target, item):
        self.calls.append((target, item))
        return {"id": "change"}

    def create_pr(self, target, item, change):
        return "pr-1"


def test_authenticated_resume_through_intake(control_env, tmp_path, monkeypatch):
    backend = Backend()
    db = tmp_path / "performance.sqlite"
    perf = Performance(str(db), backend)
    monkeypatch.setenv("AX_PERFORMANCE_DB", str(db))
    monkeypatch.setenv("AX_PERFORMANCE_TARGETS", json.dumps({"issue-automation": "repo-a", "worker-pool": "repo-b"}))
    (control_env["tokens"]["secrets"] / "core-token-intake").write_text(control_env["tokens"]["core"]["intake"])
    config = tmp_path / "intake-config"
    config.mkdir()
    entry = {"name": "control.command", "title": "control", "tier": "operate", "irreversible": False,
             "via": ["local", "hq"], "allowed_from": ["commander", "hq"],
             "schema": "schemas/dest/control.command.json", "handler": "http://control/v1/commands", "ack": "sync"}
    (config / "intake-destinations.yaml").write_text(yaml.safe_dump([entry]))

    def transport(method, url, headers, body, timeout):
        response = control_env["core"].request(method, urlsplit(url).path, headers=headers, content=body)
        return response.status_code, dict(response.headers), response.content

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    source = Path(__file__).resolve().parents[2] / "core" / "intake" / "dest"
    for name in ("b5", "z_performance"):
        (dest_dir / name).symlink_to(source / name, target_is_directory=True)
    app = build_app(config_dir=config, dest_dir=dest_dir, ext_dir=tmp_path / "no-ext",
                    transport=transport, run_heartbeat_loop=False)
    assert app.state.intake.dest_results == {"b5": None, "z_performance": None}

    def client(ident):
        async def authenticated(scope, receive, send):
            if scope["type"] == "http" and ident is not None:
                scope["ax_ident"] = ident
            await app(scope, receive, send)
        return TestClient(authenticated)

    permission = control_env["service"].create_permission(
        {"project": "ax", "repo": "repo-a", "destination": "local",
         "operations": ["plugin.run:issue-automation"], "source_instruction": "test"}, by="user")
    keys = count()

    def command(target="issue-automation", mode="run", **extra):
        return {"destination": "control.command", "idempotency_key": str(next(keys)), "payload": {"target": target, "op": "set_mode",
                "args": {"mode": mode, **({"permission_ids": [permission["id"]]}
                         if target == "issue-automation" and mode == "run" else {})},
                "reason": "resume", **extra}}

    assert client(None).post("/v1/instructions", json=command()).status_code == 403
    assert client(local_ident(1000, allowed_uid=1000)).post("/v1/instructions", json=command()).status_code == 403
    hq = client(hq_ident(2001, allowed_uid=2001))
    assert hq.post("/v1/instructions", json=command(mode="invalid")).status_code == 422
    assert hq.post("/v1/instructions", json=command(reason="")).status_code == 400
    assert hq.post("/v1/instructions", json=command(issued_by="user")).status_code in (403, 422)
    hq.post("/v1/instructions", json=command(target="all"))
    assert perf.mode("repo-a") == "draining" and perf.mode("repo-b") == "draining"
    assert perf.run("repo-a", "item", trigger="schedule")["status"] == "stopped"
    assert not backend.calls
    response = hq.post("/v1/instructions", json=command())
    assert response.status_code == 200, response.text
    assert response.json()["result"]["issued_by"] == "user"
    assert perf.mode("repo-a") == "run" and perf.mode("repo-b") == "draining"
    assert perf.run("repo-a", "item", trigger="run_now")["status"] == "sent"
    assert perf.run("repo-b", "item", trigger="run_now")["status"] == "stopped"
    assert backend.calls == [("repo-a", "item")]
    perf.stop("repo-a")
    expired = hq.post("/v1/instructions", json=command(expires_at="2000-01-01T00:00:00.000Z"))
    assert expired.status_code >= 400
    assert perf.mode("repo-a") == "draining"
    (control_env["tokens"]["secrets"] / "core-token-intake").write_text("wrong-test-token")
    assert hq.post("/v1/instructions", json=command()).status_code in (401, 403)
    assert perf.mode("repo-a") == "draining"
    (control_env["tokens"]["secrets"] / "core-token-intake").write_text(control_env["tokens"]["core"]["intake"])
    def unavailable(*args):
        raise sqlite3.OperationalError("test unavailable")
    monkeypatch.setattr(sys.modules["axintake_dest_z_performance"], "_resume", unavailable)
    assert hq.post("/v1/instructions", json=command()).status_code == 503
    assert perf.mode("repo-a") == "draining"

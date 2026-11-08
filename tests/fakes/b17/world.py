"""Fake operator world: no subprocesses, services, sockets or external writes."""
from __future__ import annotations

import json
from pathlib import Path

from axcommon.ids import new_ulid
from tools.cutover.common import CommandResult
from tools.cutover.stages import REAL_TESTS


class FakeRunner:
    def __init__(self, record_path: Path):
        self.record_path = record_path
        self.calls = []
        self.approved = list(range(1, 15))
        self.holder = True
        self.verify_ok = True
        self.note_ok = True
        self.owner = {"principal": "seat-test", "session": "test-session", "via": "local"}

    def run(self, argv, *, env=None, timeout=30):
        self.calls.append(tuple(argv))
        if list(argv[:3]) == ["ax", "approval", "verify"]:
            return CommandResult(0, json.dumps({"ok": self.verify_ok, "data": {
                "approval_id": argv[3], "kind": "removal", "state": "granted", "executed": True,
                "scope": "both", "changes": self.approved,
                "checks": {"record": "ok", "go_file": "ok", "build_go": "ok"}}}))
        if list(argv[:4]) == ["ax", "claim", "renew", "cutover"]:
            return CommandResult(0 if self.holder else 8, json.dumps({"ok": True, "data": {
                "holder": self.holder, "owner": self.owner, "claim_event_id": new_ulid()}}))
        if len(argv) >= 7 and Path(argv[2]).name == "cutover_note.py":
            if not self.note_ok:
                return CommandResult(3)
            kind, subject, target, result, reason = argv[3:8]
            event_id = new_ulid()
            event = {"id": event_id, "ts": "2026-11-07T00:00:00.000Z", "cause": new_ulid(),
                     "project": None, "subloop": None,
                     "kind": kind, "subject": subject, "source": {"component": "cutover-operator",
                     "host": "fixture", **self.owner}, "data": {"target": target, "result": result, "reason": reason}}
            if env.get("CUTOVER_AT"):
                event["data"]["at"] = env["CUTOVER_AT"]
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            with self.record_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
            return CommandResult(0, event_id + "\n")
        raise AssertionError("unexpected fake command")


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.conditions = {}
        self.todos = {}
        self.ledger_cards = {}
        self.results = {test: "ok" for test in REAL_TESTS}
        self.outputs = {"check_public_browser": True}
        self.missing = set()
        self.fail = set()
        self.modes = {"social-network": "collect_only", "chat-service": "collect_only", "scheduled-publisher": "collect_only"}

    def supports(self, action):
        return action not in self.missing

    def check(self, name, ctx):
        self.calls.append("check:" + name)
        return self.conditions.get(name, True)

    def perform(self, action, ctx, **kwargs):
        self.calls.append(action)
        if action in self.fail:
            raise RuntimeError("fake_failure")
        if action == "check_b17":
            return self.results
        if action == "create_old_todo_card":
            self.ledger_cards.setdefault(kwargs["idempotency_key"], kwargs["title"])
        if action == "resume_patrol":
            assert kwargs["keep_collect_only"] == ("social-network", "chat-service", "scheduled-publisher")
        if action == "collect_inventory":
            assert kwargs["mode"] == "collect_only"
            assert kwargs["cutover_event_id"]
        return self.outputs.get(action)

    def todo(self, key, title, ctx):
        self.todos[key] = title

"""Offline intake/work-tdd/registry adapters. No service or socket is started."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import yaml

from core.intake.dest.b12.subloop import Approval, Ports, Service, Store


class World:
    def __init__(self, tmp: Path, repo: Path):
        self.tmp, self.repo = tmp, repo
        self.items, self.todos, self.parks, self.cards = [], [], [], []
        self.events, self.accepts, self.deploys, self.validations = [], [], [], []
        self.branches, self.approvals = {}, {}
        self.head = "a" * 40
        self.registered_ids = {"other"}
        self.submit_reason = None
        self.validate_error = False
        self.accept_error = False
        self.mutate_validation = None
        self.other_modes = {"other": "run"}
        self.builds, self.starts = [], []
        self.now = datetime(2026, 11, 7, 14, 59, tzinfo=timezone.utc)
        self.store = Store(tmp / "pending.sqlite3")
        self.service = self.make_service()

    def make_service(self, **kwargs):
        ports = Ports(self.create_todo, self.submit_item, self.accept_branch, self.branch_root,
                      lambda: len(self.registered_ids), lambda value: value in self.registered_ids,
                      self.validate, self.approval, self.park, self.card, self.emit, self.read_head)
        return Service(self.store, ports, template_root=self.repo / "templates/subloop",
                       clock=lambda: self.now, **kwargs)

    def create_todo(self, record):
        self.todos.append(deepcopy(record))
        return "todo-" + record["request_id"]

    def submit_item(self, item):
        self.items.append(deepcopy(item))
        return {"status": 429, "reason": self.submit_reason} if self.submit_reason else {"status": 202}

    def accept_branch(self, registration):
        self.accepts.append(deepcopy(registration))
        if self.accept_error:
            return {"accepted": False}
        ident = registration["branch"].split("/", 1)[1]
        self.registered_ids.add(ident)
        self.deploys.append({"op": "deploy"})
        return {"accepted": True, "sha": "accepted-candidate"}

    def read_head(self, repository, branch):
        return self.head

    def branch_root(self, branch):
        return self.branches.get(branch)

    def validate(self, body):
        self.validations.append(deepcopy(body))
        if self.mutate_validation:
            self.mutate_validation(body)
        return {"ok": not self.validate_error, "errors": ["invalid"] if self.validate_error else []}

    def approval(self, key, request, ident):
        return self.approvals.get(key, Approval("pending-" + key, False))

    def park(self, todo_id, reason, check_at):
        self.parks.append((todo_id, reason, check_at))

    def card(self, todo_id, body):
        self.cards.append((todo_id, deepcopy(body)))

    def emit(self, kind, subject, body):
        self.events.append({"kind": kind, "subject": subject, "data": deepcopy(body)})

    def candidate(self, ident, *, secrets=None, outputs=None, sensitivity="public", capabilities=None):
        root = self.tmp / ("branch-" + ident)
        self.branches["subloop/" + ident] = root
        directory = root / "plugins/subloop" / ident
        directory.mkdir(parents=True, exist_ok=True)
        manifest = yaml.safe_load((self.repo / "templates/subloop/manifest.yaml").read_text())
        manifest.update(id=ident, kind="subloop", sensitivity=sensitivity,
                        secrets=secrets or [], outputs=outputs or [], roles=[],
                        capabilities=capabilities or ["run", "draining"], execution="mechanical")
        (directory / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        graph = {"schema_version": 1, "entry": "done", "nodes": [
            {"id": "done", "type": "shell.run", "with": {"cmd": "true"},
             "limits": {"timeout_s": 30}, "on_fail": "eject"}], "edges": []}
        (directory / "graph.yaml").write_text(yaml.safe_dump(graph), encoding="utf-8")
        fragments = root / "plugins/registry.d"
        fragments.mkdir(parents=True, exist_ok=True)
        (fragments / "other.yaml").write_text(yaml.safe_dump({"plugins": [
            {"id": "other", "kind": "subloop", "path": "plugins/subloop/other/manifest.yaml"}]}))
        (root / "plugins/registry.yaml").write_text(yaml.safe_dump({"schema_version": 1, "plugins": [
            {"id": "other", "kind": "subloop", "path": "plugins/subloop/other/manifest.yaml", "source": "other.yaml"}]}))
        return root

    def completed(self, key, *, ident="new-one", **changes):
        data = {"state": "completed", "todo_id": "todo-" + key, "item_id": "todo-" + key,
                "artifacts": {"branch": "subloop/" + ident, "remote": "work.git"}}
        data.update(changes)
        return {"kind": "item.state", "subloop": "work-tdd", "source": {"component": "work-tdd"}, "data": data}

    def assert_local_only(self):
        assert self.other_modes == {"other": "run"}
        assert not self.builds and not self.starts

    def assert_no_registration(self):
        assert self.registered_ids == {"other"}
        assert not self.accepts and not self.deploys
        self.assert_local_only()

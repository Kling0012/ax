"""Subloop creation orchestration (S-6.21, S-7.10, F-18.3).

All ports are intake-owned adapters. No subprocess, network, build or start is
performed here. In particular approval evidence must not be decoded from JSON.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from core.intake.vias import Ident

from .policy import Rejected, decide, within_declaration
from .registration import attribution, commit_message, plugin_id, read_candidate, stage_registration
from .store import Store


@dataclass(frozen=True)
class Request:
    """Internal normalized input; deliberately NOT a new destination wire schema."""
    plugin_id: str
    project: str
    title: str
    body: str
    spec: dict
    acceptance: dict
    quote: str | None = None


@dataclass(frozen=True)
class Approval:
    approval_id: str | None = None
    granted: bool = False


@dataclass
class Ports:
    create_todo: Callable  # (record) -> todo_id; idempotent by request_id
    submit_item: Callable  # common.submit_item adapter: (item) -> {status, reason?}
    accept_branch: Callable  # common.accept_branch adapter: (registration) -> {accepted, sha}
    branch_root: Callable  # (branch) -> intake-owned immutable candidate Path, or None
    registered_count: Callable  # () -> registered subloop count, not all plugin kinds
    registered: Callable  # (plugin_id) -> bool
    validate: Callable  # ({kind, body}) -> {ok, errors}; control POST /v1/validate
    approval: Callable  # (request_id, Request, Ident) -> Approval; trusted approval store
    park: Callable  # (todo_id, reason, check_at|None)
    card: Callable  # (todo_id, card); idempotent by todo_id + waiting_reason
    emit: Callable  # (kind, subject, data); durable event outbox
    read_head: Callable  # (repository, branch) -> full commit SHA; raises on failure


class Service:
    def __init__(self, store: Store, ports: Ports, *, template_root: Path,
                 daily_max: int = 3, total_max: int = 16,
                 day_tz: str = "Asia/Tokyo", clock=None, stage=stage_registration):
        self.store, self.ports = store, ports
        self.template_root = Path(template_root)
        self.daily_max, self.total_max = daily_max, total_max
        self.day_tz = ZoneInfo(day_tz)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stage = stage

    def _reply(self, record: dict) -> tuple[int, dict]:
        if record["phase"] == "awaiting_approval":
            return 202, {"reason": "approval_required", "exit_code": 4,
                         "approval_id": record.get("pending_approval_id")}
        return 202, {"todo_id": record.get("todo_id"), "branch": record["branch"], "state": record["phase"]}

    def _park(self, record: dict, reason: str, *, retry_hour=False) -> tuple[int, dict]:
        record.update(phase="parked", reason=reason)
        self.store.save(record)
        check_at = (self.clock() + timedelta(hours=1)).isoformat() if retry_hour else None
        self.ports.park(record["todo_id"], reason, check_at)
        self.ports.card(record["todo_id"], {"reason": reason, "branch": record["branch"], **attribution(record)})
        return self._reply(record)

    def create(self, request_id: str, request: Request, ident: Ident) -> tuple[int, dict]:
        """Only verified transport identity and server-side approval evidence are used."""
        try:
            return self._create(request_id, deepcopy(request), ident)
        except Rejected as exc:
            self.ports.emit("instruction.rejected", request_id, {"destination": "subloop.create", "reason": exc.reason})
            return exc.status, {"reason": exc.reason}

    def _create(self, request_id: str, request: Request, ident: Ident, *, approved_id=None) -> tuple[int, dict]:
        decision = decide(request.spec, request.quote, ident)
        ident_id = plugin_id(request.plugin_id)
        if not isinstance(request_id, str) or not request_id:
            raise Rejected(400, "idempotency_required")
        if not isinstance(request.acceptance, dict) or any(
            not isinstance(request.acceptance.get(key), list) or not request.acceptance[key]
            or any(not isinstance(v, str) or not v.strip() for v in request.acceptance[key])
            for key in ("examples", "boundaries")
        ):
            raise Rejected(400, "bad_request")
        fingerprint = json.dumps(asdict(request), sort_keys=True, ensure_ascii=False)
        with self.store.lock:
            record = self.store.get(request_id)
            if record:
                if record["fingerprint"] != fingerprint or record["created_by"] != decision.created_by:
                    raise Rejected(409, "idempotency_mismatch")
                if record["phase"] != "awaiting_approval":
                    return self._reply(record)
            elif self.ports.registered(ident_id):
                raise Rejected(409, "subloop_exists")
            if self.ports.registered_count() >= self.total_max:
                raise Rejected(429, "subloop_limit")
            evidence = (Approval(approved_id, True) if approved_id else
                        self.ports.approval(request_id, request, ident) if decision.tier == "irreversible" else Approval())
            if evidence.granted and ((not decision.hq and not approved_id) or not evidence.approval_id):
                # An irreversible seat may request but cannot grant an approval.
                raise Rejected(403, "tier_insufficient")
            new = {
                "request_id": request_id, "fingerprint": fingerprint,
                "plugin_id": ident_id, "branch": f"subloop/{ident_id}",
                "request": asdict(request), "tier": decision.tier,
                "created_by": decision.created_by, "quote": decision.quote,
                "approval_id": evidence.approval_id if evidence.granted else None,
                "pending_approval_id": evidence.approval_id,
                "phase": "awaiting_approval" if decision.tier == "irreversible" and not evidence.granted else "admitted",
            }
            if record is None:
                record = self.store.admit(new, day=self.clock().astimezone(self.day_tz).date().isoformat(),
                                          hq=decision.hq, daily_max=self.daily_max)
                if record["fingerprint"] != fingerprint or record["created_by"] != decision.created_by:
                    raise Rejected(409, "idempotency_mismatch")
            elif evidence.granted:
                record.update(phase="admitted", approval_id=evidence.approval_id)
                self.store.save(record)
            if record["phase"] == "awaiting_approval":
                return self._reply(record)
            record["todo_id"] = self.ports.create_todo(deepcopy(record))
            self.store.save(record)
            # Only an approved HQ resubmission may reuse an existing branch.
            existing = self.ports.branch_root(record["branch"])
            if decision.hq and evidence.granted and existing is not None:
                record["phase"] = "queued"
                self.store.save(record)
                return self._finish(record)
            if existing is not None:
                return self._park(record, "subloop_branch_exists")
            item = {
                "item_key": record["todo_id"], "todo_id": record["todo_id"], "kind": "subloop_create",
                "project": request.project, "role": "impl", "title": request.title,
                "body": request.body + "\n\n" + json.dumps({"spec": request.spec, **attribution(record)}, ensure_ascii=False),
                "acceptance": deepcopy(request.acceptance), "base_ref": "main", "branch": record["branch"],
                "template": "templates/subloop/",
            }
            record.update(phase="submitting", item=item)
            self.store.save(record)
            try:
                result = self.ports.submit_item(item)
            except Exception:
                return self._park(record, "submission_uncertain")
            if result.get("reason") == "inbox_full":
                return self._park(record, "inbox_full", retry_hour=True)
            if result.get("status", 500) >= 400:
                return self._park(record, "submission_failed")
            record["phase"] = "queued"
            self.store.save(record)
            return self._reply(record)

    def on_item_state(self, event: dict) -> tuple[int, dict] | None:
        """Consumes trusted timeline events only, never an HTTP request body."""
        if (event.get("kind") != "item.state" or event.get("subloop") != "work-tdd"
                or (event.get("source") or {}).get("component") != "work-tdd"):
            return None
        data = event.get("data") or {}
        with self.store.lock:
            record = self.store.for_todo(data.get("todo_id"))
            if record is None or data.get("item_id") != record.get("todo_id"):
                return None
            if record["phase"] == "registered" and data.get("state") == "completed":
                if not record.get("range_card_written"):
                    self._notify(record)
                return None
            if record["phase"] != "queued":
                return None
            if data.get("state") == "ejected":
                return self._park(record, "item_ejected")
            if data.get("state") != "completed":
                return None
            artifacts = data.get("artifacts") or {}
            if artifacts.get("branch") != record["branch"] or artifacts.get("remote") != "work.git":
                return self._park(record, "candidate_branch_mismatch")
            return self._finish(record, repository=artifacts["remote"], branch=artifacts["branch"])

    def _finish(self, record: dict, *, repository="work.git", branch=None) -> tuple[int, dict]:
        branch = branch or record["branch"]
        if repository != "work.git" or not branch.startswith("subloop/") or branch != record["branch"]:
            return self._park(record, "candidate_branch_mismatch")
        try:
            sha = self.ports.read_head(repository, branch)
            if not isinstance(sha, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
                return self._park(record, "candidate_head_unavailable")
            record.update(candidate_repository=repository, candidate_branch=branch, candidate_sha=sha)
            self.store.save(record)
        except Exception:
            return self._park(record, "candidate_head_unavailable")
        try:
            root = self.ports.branch_root(record["branch"])
            if root is None:
                return self._park(record, "candidate_missing")
            root = Path(root)
            manifest, graph = read_candidate(root, record["plugin_id"])
            for kind, body in (("manifest", manifest), ("graph", {"manifest": manifest, "graph": graph})):
                validation = self.ports.validate({"kind": kind, "body": deepcopy(body)})
                if validation.get("ok") is not True or validation.get("errors"):
                    return self._park(record, "graph_validation_failed")
            # Re-read the files immediately before staging. JSON from an item
            # result can never replace these reads.
            actual, actual_graph = read_candidate(root, record["plugin_id"])
            if not within_declaration(actual, record["request"]["spec"], record["tier"]):
                return self._park(record, "manifest_exceeds_declaration")
            if actual != manifest or actual_graph != graph:
                return self._park(record, "candidate_changed")
            if self.ports.registered_count() >= self.total_max:
                return self._park(record, "subloop_limit")
            if self.ports.registered(record["plugin_id"]):
                return self._park(record, "subloop_exists")
            try:
                current_sha = self.ports.read_head(repository, branch)
            except Exception:
                return self._park(record, "candidate_head_unavailable")
            if not isinstance(current_sha, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", current_sha):
                return self._park(record, "candidate_head_unavailable")
            if current_sha != sha:
                return self._park(record, "candidate_head_changed")
            paths = self.stage(root, record["plugin_id"], actual, template_root=self.template_root)
            record["phase"] = "accepting"
            self.store.save(record)
            accepted = self.ports.accept_branch({
                "branch": record["branch"], "todo_id": record["todo_id"], "candidate_sha": sha,
                "plugin_id": record["plugin_id"], "declaration": deepcopy(record["request"]["spec"]),
                "tier": record["tier"], "paths": paths, "message": commit_message(record), **attribution(record),
            })
            if accepted.get("accepted") is not True or not accepted.get("sha"):
                return self._park(record, accepted.get("reason") or "acceptance_failed")
            record.update(phase="registered", sha=accepted["sha"], knobs=deepcopy(manifest.get("knobs", {})))
            self.store.save(record)
        except NotImplementedError:
            # Missing production bindings are explicit, not simulated success.
            raise
        except Rejected as exc:
            return self._park(record, exc.reason)
        except Exception:
            return self._park(record, "acceptance_uncertain")
        self._notify(record)
        return self._reply(record)

    def _notify(self, record: dict) -> None:
        if not record.get("event_written"):
            self.ports.emit("plugin.registered", record["plugin_id"], attribution(record))
            record["event_written"] = True
            self.store.save(record)
        creator = ("利用者が作成" if record["created_by"] == "user"
                   else f"司令塔 {record['created_by']} が作成")
        self.ports.card(record["todo_id"], {
            "waiting_reason": "range_review", "created_by_label": creator,
            "knobs": deepcopy(record.get("knobs", {})), **attribution(record),
        })
        record["range_card_written"] = True
        self.store.save(record)

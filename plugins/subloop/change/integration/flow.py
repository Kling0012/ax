"""Change routes and merge integration. Every wait returns to the caller."""
from __future__ import annotations

import json
from typing import Callable

from .delivery import Delivery
from .gate import Decision, MergeGate, full_sha, independent_safe, requirements_complete, valid_verification
from .journal import Journal, Learning
from .ports import DependencyUnavailable, Ports


def item_key(item: dict) -> str:
    fields = {k: item[k] for k in ("provider", "issue_repo", "kind", "number")}
    fields["issue_repo"] = fields["issue_repo"].lower()
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


class ChangeFlow:
    def __init__(self, ports: Ports, journal: Journal, targets: list[dict], *, clock: Callable,
                 fix_rounds: int = 3, starts_per_cycle: int = 4,
                 model_calls_per_cycle: int = 30, cycle_seconds: int = 300):
        self.ports, self.journal, self.clock = ports, journal, clock
        self.fix_rounds, self.starts_per_cycle = fix_rounds, starts_per_cycle
        self.model_calls_per_cycle, self.cycle_seconds = model_calls_per_cycle, cycle_seconds
        self.targets, self.invalid_targets = {}, set()
        for target in targets:
            key = target["issue_repo"].lower()
            if key in self.targets or key in self.invalid_targets:
                self.targets.pop(key, None)
                self.invalid_targets.add(key)
            else:
                self.targets[key] = target
        self._local_change_ledger = getattr(journal, "change_ledger", None) is None
        if self._local_change_ledger:
            from core.intake.dest.b12b3.change import ChangeLedger
            path = journal.db.execute("PRAGMA database_list").fetchone()[2]
            journal.change_ledger = ChangeLedger(path, self.targets)
        self.gate = MergeGate()
        self.learning = Learning(journal)
        self.notices = Delivery(journal, ports.notifications, outbound_check=ports.outbound_check, clock=clock)
        self.publications = Delivery(journal, ports.publications, outbound_check=ports.outbound_check, clock=clock)
        self.releases = Delivery(journal, ports.release, outbound_check=ports.outbound_check, clock=clock)
        self.model_calls = 0

    def stop(self, item: dict, reason: str):
        self.journal.put("stopped", item_key(item), {"stopped": True, "reason": reason})

    def resume(self, item: dict):
        self.journal.put("stopped", item_key(item), {"stopped": False})

    def route(self, item: dict, decision: Decision, *, dry_run: bool = False, artifacts=None) -> dict:
        data = {"reason": decision.reason, "target": item_key(item), "result": decision.route,
                "next_check": decision.next_check, "dry_run": dry_run}
        event = self.journal.event("change.route", item_key(item), data, project=item.get("project"))
        if not dry_run:
            state = decision.route if decision.route in {"queued", "running", "waiting_user", "retry_wait", "succeeded", "parked"} else "parked"
            self.ports.collection.checkpoint(item, state, decision.reason)
        if decision.reason not in {"ready", "collected", "dry_run", "question_answered", "release_pending"}:
            self.learning.record({"id": f"change:{event}", "source_event": event,
                                  "project": item.get("project", "unknown"), "reason": decision.reason,
                                  "evidence_ref": event, "status": "pending"})
        return {"status": decision.route, "reason": decision.reason, "next_check": decision.next_check,
                "dry_run": dry_run, "artifacts": artifacts or {}}

    def _permission(self, item: dict, operation: str, repo: str | None = None) -> dict:
        return self.ports.permissions.check(project=item["project"], repo=repo or item["implementation_repo"],
                                            destination="github", operation=operation)

    def _decision(self, item: dict, target: dict, snapshot: dict, safety: dict, verification: dict) -> Decision:
        online = getattr(self.ports.permissions, "online_instruction", None)
        instruction = (online(item, repo=item["implementation_repo"], pr=item["pr"], head_sha=snapshot["head_sha"],
                              input_revision=snapshot["input_revision"]) if callable(online) else
                       self.ports.permissions.instruction(item, repo=item["implementation_repo"],
                                                          pr=item["pr"], head_sha=snapshot["head_sha"]))
        notification = self.journal.get("notice", self._merge_key(item))
        return self.gate.evaluate(item, target, snapshot, safety, verification, self._permission(item, "merge"),
                                  now=self.clock(), instruction=instruction, notification=notification)

    def _approval(self, item, snapshot, *, operation, repo, pr):
        authorize = getattr(self.ports.permissions, "authorize_scoped", None)
        if not callable(authorize):
            return True
        try:
            result = authorize(item, repo=repo, pr=pr, head_sha=snapshot.get("head_sha"),
                               input_revision=snapshot.get("input_revision"), operation=operation)
            return isinstance(result, dict) and result.get("status") == "approved"
        except Exception:
            return False

    @staticmethod
    def _merge_key(item):
        pr = item.get("pr")
        suffix = str(pr) if type(pr) is int and pr > 0 else "pending:" + item_key(item)
        return f"merge:{item['implementation_repo'].lower()}:{suffix}"

    def _handoff(self, item: dict, merge: dict) -> dict:
        body = {"repo": item["implementation_repo"], "pr": item["pr"], "head_sha": merge["head_sha"],
                "merge_commit_sha": merge["merge_commit_sha"],
                "issue": {k: item[k] for k in ("provider", "issue_repo", "kind", "number")}}
        key = f"release:{item['implementation_repo'].lower()}:{item['pr']}"
        delivered = self.releases.deliver(key, "release", body)
        if delivered.get("status") != "delivered":
            return self.route(item, Decision(False, "release_handoff_pending", "retry_wait", delivered.get("next_retry")))
        # Issue closing and release execution belong to the release subloop.
        return self.route(item, Decision(True, "release_pending", "succeeded"), artifacts={"release": body})

    def _existing_merge(self, item: dict) -> dict | None:
        if type(item.get("pr")) is not int or item["pr"] <= 0:
            return None
        key = self._merge_key(item)
        op = self.journal.operation(key)
        if not op:
            return None
        if op["state"] == "succeeded":
            return self._handoff(item, op["result"])
        if op["state"] == "failed":
            return None
        if op["state"] == "launching" and self.clock() < op["body"]["lease_until"]:
            return self.route(item, Decision(False, "merge_in_flight", "retry_wait", op["body"]["lease_until"]))
        try:
            observed = self.ports.github.reconcile_merge(item["implementation_repo"], item["pr"], sha=op["body"]["head_sha"])
        except Exception:
            observed = {}
        if self._merged(observed, op["body"]["head_sha"]):
            self.journal.finish(key, "succeeded", observed)
            return self._handoff(item, observed)
        self.journal.finish(key, "lost", {"reason": "merge_unconfirmed"})
        return self.route(item, Decision(False, "merge_unconfirmed", "parked"))

    @staticmethod
    def _merged(result: dict, head: str) -> bool:
        return isinstance(result, dict) and result.get("merged") is True and result.get("head_sha") == head and full_sha(result.get("merge_commit_sha"))

    def merge(self, item: dict, target: dict, safety: dict, verification: dict) -> dict:
        existing = self._existing_merge(item)
        if existing is not None:
            return existing
        self.ports.collection.checkpoint(item, "running", "merge_preflight")
        try:
            snapshot = self.ports.github.view(item, target)
        except Exception:
            return self.route(item, Decision(False, "pr_state_unavailable"))
        if not self._approval(item, snapshot, operation="merge", repo=item["implementation_repo"], pr=item["pr"]):
            return self.route(item, Decision(False, "individual_approval_required", "waiting_user"))
        decision = self._decision(item, target, snapshot, safety, verification)
        if decision.route == "waiting_user" and not callable(getattr(self.ports.permissions, "authorize_scoped", None)):
            self.ports.permissions.wait(item, repo=item["implementation_repo"], pr=item["pr"],
                                        head_sha=snapshot["head_sha"], reason=decision.reason)
        if decision.route == "notify":
            online = getattr(self.ports.permissions, "online_instruction", None)
            instruction = (online(item, repo=item["implementation_repo"], pr=item["pr"], head_sha=snapshot["head_sha"],
                                  input_revision=snapshot["input_revision"]) if callable(online) else
                           self.ports.permissions.instruction(item, repo=item["implementation_repo"],
                                                              pr=item["pr"], head_sha=snapshot["head_sha"]))
            if not instruction:
                return self.route(item, Decision(False, "individual_approval_required", "waiting_user"))
            body = {"repo": item["implementation_repo"], "pr": item["pr"], "head_sha": snapshot["head_sha"],
                    "instruction_ref": instruction["instruction_ref"], "operation": "merge", "wait_seconds": 1800}
            key = f"notice:{self._merge_key(item)}:{snapshot['head_sha']}:{instruction['instruction_ref']}"
            result = self.notices.deliver(key, "user", body)
            notice = {**result, "head_sha": snapshot["head_sha"], "instruction_ref": instruction["instruction_ref"]}
            self.journal.put("notice", self._merge_key(item), notice)
            decision = self._decision(item, target, snapshot, safety, verification)
        if not decision.allowed:
            return self.route(item, decision)
        # A second fresh read catches comments/requirements/CI/head/permission
        # changes during preparation or notification. The write also CASes SHA.
        self.ports.collection.checkpoint(item, "running", "merge_preflight")
        try:
            snapshot = self.ports.github.view(item, target)
        except Exception:
            return self.route(item, Decision(False, "pr_state_unavailable"))
        decision = self._decision(item, target, snapshot, safety, verification)
        if not decision.allowed:
            return self.route(item, decision)
        head = snapshot["head_sha"]
        body = {"repo": item["implementation_repo"], "pr": item["pr"], "head_sha": head,
                "method": target["merge_method"]}
        if self.ports.outbound_check(body) is not True:
            return self.route(item, Decision(False, "outbound_check_failed"))
        ledger = getattr(self.journal, "change_ledger", None)
        try:
            if self._local_change_ledger:
                ledger.targets = set(self.targets)
            row = ledger.for_item(item) if ledger else None
            if (not row or row["issue_repo"] != item["issue_repo"].lower()
                    or row["kind"] != item.get("kind")
                    or row["number"] != item.get("number")):
                return self.route(item, Decision(False, "change_request_unmatched"))
            request_id = row["request_id"]
            if row["state"] != "waiting":
                return self.route(item, Decision(False, "merge_not_waiting"))
            if (type(snapshot.get("pr")) is not int or snapshot["pr"] != item.get("pr")
                    or row["pr_number"] != snapshot["pr"] or row["head_sha"] != head):
                return self.route(item, Decision(False, "pr_identity_mismatch"))
            if not ledger.transition(request_id, "waiting", "merging", pr_number=snapshot["pr"], head_sha=head):
                return self.route(item, Decision(False, "merge_not_waiting"))
        except Exception:
            return self.route(item, Decision(False, "change_ledger_unavailable"))
        key = self._merge_key(item)
        if not self.journal.claim(key, "merge", body, retry_failed=True):
            ledger.transition(request_id, "merging", "unknown", "merge_journal_conflict")
            return self.route(item, Decision(False, "merge_unconfirmed"))
        self.journal.event("change.merge", item_key(item), {**body, "state": "launching"})
        try:
            current = self.ports.github.view(item, target)
            current_bound = (current.get("head_sha") == snapshot["head_sha"] and
                             current.get("input_revision") == snapshot["input_revision"])
        except Exception:
            current_bound = False
        if not current_bound or not self._approval(item, snapshot, operation="merge", repo=item["implementation_repo"], pr=item["pr"]):
            ledger.transition(request_id, "merging", "waiting", "individual_approval_required")
            self.journal.finish(key, "failed", {"reason": "individual_approval_required"})
            return self.route(item, Decision(False, "individual_approval_required", "waiting_user"))
        try:
            final_decision = self._decision(item, target, current, safety, verification)
        except Exception:
            final_decision = Decision(False, "merge_preflight_unavailable")
        if not final_decision.allowed:
            ledger.transition(request_id, "merging", "waiting", final_decision.reason)
            self.journal.finish(key, "failed", {"reason": final_decision.reason})
            return self.route(item, final_decision)
        try:
            result = self.ports.github.merge(item["implementation_repo"], item["pr"], sha=head, method=target["merge_method"])
        except Exception:
            ledger.transition(request_id, "merging", "unknown", "merge_unconfirmed")
            self.journal.finish(key, "lost", {"reason": "merge_unconfirmed"})
            return self.route(item, Decision(False, "merge_unconfirmed"))
        if not self._merged(result, head):
            if isinstance(result, dict) and result.get("merged") is False and result.get("definitive") is True:
                ledger.transition(request_id, "merging", "waiting", "merge_failed")
                self.journal.finish(key, "failed", {"reason": "merge_failed"})
                return self.route(item, Decision(False, "merge_failed"))
            ledger.transition(request_id, "merging", "unknown", "merge_unconfirmed")
            self.journal.finish(key, "lost", {"reason": "merge_unconfirmed"})
            return self.route(item, Decision(False, "merge_unconfirmed"))
        ledger.transition(request_id, "merging", "merged", "merge_succeeded")
        self.journal.finish(key, "succeeded", result)
        self.journal.event("change.merge", item_key(item), {**body, "state": "succeeded", "merge_commit_sha": result["merge_commit_sha"]})
        return self._handoff(item, result)

    def publish(self, item, target, safety, snapshot):
        prepared = self._work(item, target, "prepare_publication", safety=safety)
        body = prepared.get("publication")
        if not isinstance(body, dict) or body.get("head_sha") != snapshot["head_sha"]:
            return {"status": "blocked", "reason": "publication_unavailable"}
        # Publication routing is pinned by the registry, not by generated prose.
        body = {**body, "repo": item["implementation_repo"], "pr": item.get("pr"), "source_item": item_key(item)}
        if not self._approval(item, snapshot, operation="pr_update", repo=item["implementation_repo"], pr=item.get("pr")):
            return {"status": "blocked", "reason": "individual_approval_required"}
        if self._permission(item, "pr_update").get("allowed") is not True:
            if not callable(getattr(self.ports.permissions, "authorize_scoped", None)):
                self.ports.permissions.wait(item, repo=item["implementation_repo"], pr=item.get("pr"),
                                            head_sha=snapshot["head_sha"], reason="publication_permission_missing")
            return {"status": "blocked", "reason": "permission_missing"}
        self.ports.collection.checkpoint(item, "running", "publication_preflight")
        try:
            current = self.ports.github.view(item, target)
        except Exception:
            return {"status": "blocked", "reason": "pr_state_unavailable"}
        if (current.get("head_sha") != snapshot.get("head_sha") or
                current.get("input_revision") != snapshot.get("input_revision")):
            return {"status": "blocked", "reason": "approval_scope_stale"}
        if not self._approval(item, current, operation="pr_update", repo=item["implementation_repo"], pr=item.get("pr")):
            return {"status": "blocked", "reason": "individual_approval_required"}
        return self.publications.deliver(f"pr:{self._merge_key(item)}:{snapshot['head_sha']}:{snapshot['input_revision']}", "github", body)

    @staticmethod
    def adopt_publication(item, receipt):
        if type(item.get("pr")) is int and item["pr"] > 0:
            return item
        if (type(receipt.get("pr")) is not int or receipt["pr"] <= 0 or
                receipt.get("repo", "").lower() != item["implementation_repo"].lower()):
            raise DependencyUnavailable("created_pr_unconfirmed")
        return {**item, "pr": receipt["pr"]}

    def _safety(self, item, snapshot):
        if self.model_calls + 2 > self.model_calls_per_cycle:
            raise DependencyUnavailable("model_budget_exhausted")
        self.model_calls += 2
        return self.ports.safety.review({**item, "snapshot": snapshot})

    def _work(self, item, target, operation, *, safety):
        if self.model_calls + 1 > self.model_calls_per_cycle:
            raise DependencyUnavailable("model_budget_exhausted")
        # Only independently inspected summaries reach a worker. Raw title,
        # body, comments and diff remain with the safety adapter.
        worker_input = safety.get("worker_input")
        if not isinstance(worker_input, dict):
            raise DependencyUnavailable("neutral_summary_unavailable")
        safe_item = {k: item[k] for k in ("provider", "issue_repo", "kind", "number", "implementation_repo", "pr", "project", "repair_reason", "workdir_id", "branch") if k in item}
        safe_item["worker_input"] = worker_input
        safe_item["model_budget"] = self.model_calls_per_cycle - self.model_calls
        result = self.ports.work.run(safe_item, target, operation=operation)
        calls = result.get("model_calls", 1) if isinstance(result, dict) else 1
        self.model_calls += max(1, calls) if type(calls) is int else 1
        if not isinstance(result, dict) or result.get("status") != "passed":
            raise DependencyUnavailable("work_unavailable")
        return result

    def process(self, item: dict, *, dry_run: bool | None = None, mode: str = "collect_only") -> dict:
        dry = dry_run is not False
        try:
            target = self.targets.get(item["issue_repo"].lower())
            if target is None or item.get("implementation_repo", "").lower() != target["implementation_repo"].lower():
                return self.route(item, Decision(False, "target_unavailable"), dry_run=dry)
            if (self.journal.get("stopped", item_key(item)) or {}).get("stopped"):
                return self.route(item, Decision(False, "item_stopped"), dry_run=dry)
            if dry:
                return self.route(item, Decision(False, "dry_run", "planned"), dry_run=True,
                                  artifacts={"plan": ["safety", item.get("route", "pr"), "verify", "publication", "merge", "release"],
                                             "notes": "No external side effects; no work started."})
            if mode == "collect_only":
                return self.route(item, Decision(False, "collected", "queued"))
            if mode != "run":
                return self.route(item, Decision(False, "mode_does_not_start", "queued"))
            if item.get("route") != "question":
                existing = self._existing_merge(item)
                if existing is not None:
                    return existing
            if item.get("route") == "question":
                return self._question(item, target)
            if item.get("route") in {"change", "bug"}:
                # WorkPort must use the tdd-change subgraph for code changes.
                # Safety always precedes the first worker; the graph follows the
                # same order, with the literal tdd-change include.
                snapshot = self.ports.github.view(item, target)
                safety = self._safety(item, snapshot)
                if not independent_safe(safety, head=snapshot.get("head_sha"), revision=snapshot.get("input_revision")):
                    return self.route(item, Decision(False, "safety_failed"))
                if item["route"] == "bug":
                    investigation = self._work(item, target, "investigate", safety=safety)
                    if investigation.get("cause_known") is not True:
                        return self.route(item, Decision(False, "diagnostic_required", "parked"), artifacts=investigation)
                self._work(item, target, "tdd-change", safety=safety)
            for round_number in range(self.fix_rounds + 1):
                snapshot = self.ports.github.view(item, target)
                safety = self._safety(item, snapshot)
                if not independent_safe(safety, head=snapshot.get("head_sha"), revision=snapshot.get("input_revision")):
                    return self.route(item, Decision(False, "safety_failed"))
                verification = self.ports.verification.verify(item, target, head_sha=snapshot["head_sha"], base_sha=snapshot["base_sha"])
                if verification.get("status") == "parked":
                    return self.route(item, Decision(False, verification.get("reason", "verify_unavailable")))
                verified = valid_verification(
                    verification, repo=item["implementation_repo"], base=snapshot["base_sha"],
                    head=snapshot["head_sha"], untrusted=item.get("author") not in target["trusted_authors"],
                )
                requirements_ready = requirements_complete(snapshot.get("requirements"), snapshot.get("required_ids"))
                has_pr = type(item.get("pr")) is int and item["pr"] > 0
                decision = (self._decision(item, target, snapshot, safety, verification)
                            if has_pr else Decision(False, "publication_required", "publish"))
                repair = not verified or not requirements_ready or decision.route in {"fix", "resolve_conflict"}
                if not repair:
                    if decision.route == "publish":
                        published = self.publish(item, target, safety, snapshot)
                        if published["status"] != "delivered":
                            waiting = "parked" if published["status"] == "blocked" else "retry_wait"
                            return self.route(item, Decision(False, published.get("reason", "publication_pending"), waiting, published.get("next_retry")))
                        item = self.adopt_publication(item, published)
                    return self.merge(item, target, safety, verification)
                if round_number >= self.fix_rounds:
                    trusted = item.get("author") in target.get("trusted_authors", [])
                    reason = "trusted_internal_decision_required" if trusted else "untrusted_fix_limit"
                    return self.route(item, Decision(False, reason), artifacts={"verification": verification})
                reason = "resolve_conflict" if decision.route == "resolve_conflict" else "fix"
                ev = self.journal.event("change.repair", item_key(item), {"reason": reason, "round": round_number + 1, "head_sha": snapshot["head_sha"]})
                self.learning.record({"id": f"change:{ev}", "source_event": ev, "project": item["project"],
                                      "reason": reason, "evidence_ref": ev, "status": "pending"})
                self._work({**item, "repair_reason": reason}, target, "tdd-change", safety=safety)
            raise AssertionError("bounded repair loop fell through")
        except Exception as exc:
            # Never record exception messages: adapters may include credentials.
            reason = "dependency_unavailable" if isinstance(exc, DependencyUnavailable) else "integration_unavailable"
            try:
                return self.route(item, Decision(False, reason, "retry_wait", self.clock() + 60))
            except Exception:
                return {"status": "retry_wait", "reason": reason, "dry_run": dry, "next_check": self.clock() + 60, "artifacts": {}}

    def _question(self, item, target):
        snapshot = self.ports.github.view(item, target)
        safety = self._safety(item, snapshot)
        if not independent_safe(safety, head=snapshot.get("head_sha"), revision=snapshot.get("input_revision")):
            return self.route(item, Decision(False, "safety_failed"))
        result = self._work(item, target, "answer", safety=safety)
        body = {"repo": item["issue_repo"], "number": item["number"], "body": result.get("answer", "Unknown"),
                "evidence": result.get("evidence", []), "input_revision": snapshot["input_revision"]}
        if not self._approval(item, snapshot, operation="comment", repo=item["issue_repo"], pr=None):
            return self.route(item, Decision(False, "individual_approval_required", "waiting_user"))
        if self._permission(item, "comment", repo=item["issue_repo"]).get("allowed") is not True:
            if not callable(getattr(self.ports.permissions, "authorize_scoped", None)):
                self.ports.permissions.wait(item, repo=item["issue_repo"], pr=None,
                                            head_sha=snapshot.get("head_sha"), reason="comment_permission_missing")
            return self.route(item, Decision(False, "permission_missing", "waiting_user"))
        try:
            current = self.ports.github.view(item, target)
        except Exception:
            return self.route(item, Decision(False, "pr_state_unavailable"))
        if (current.get("head_sha") != snapshot.get("head_sha") or
                current.get("input_revision") != snapshot.get("input_revision")):
            return self.route(item, Decision(False, "approval_scope_stale", "waiting_user"))
        if not self._approval(item, current, operation="comment", repo=item["issue_repo"], pr=None):
            return self.route(item, Decision(False, "individual_approval_required", "waiting_user"))
        delivery = self.publications.deliver(f"answer:{item_key(item)}:{snapshot['input_revision']}", "github", body)
        if delivery.get("status") != "delivered":
            return self.route(item, Decision(False, "answer_pending", "retry_wait", delivery.get("next_retry")))
        self.journal.event("change.answer", item_key(item), {"answered": True, "evidence": result.get("evidence", [])})
        return self.route(item, Decision(True, "question_answered", "succeeded"), artifacts={"answer": body})

    def patrol(self, *, dry_run: bool | None = None, mode: str = "collect_only") -> list[dict]:
        started = self.clock()
        self.model_calls = 0
        counts = {"targets": len(self.targets), "found": 0, "started": 0, "held": 0, "model_calls": 0, "elapsed_s": 0}
        self.journal.event("change.cycle", "change", {"phase": "started", **counts})
        results = []
        try:
            items = self.ports.collection.collect(dry_run=dry_run is not False, mode=mode)
            counts["found"] = len(items)
            for item in items:
                if counts["started"] >= self.starts_per_cycle or self.model_calls + 3 > self.model_calls_per_cycle or self.clock() - started >= self.cycle_seconds:
                    result = self.route(item, Decision(False, "cycle_budget", "queued"), dry_run=dry_run is not False)
                else:
                    result = self.process(item, dry_run=dry_run, mode=mode)
                    if dry_run is False and mode == "run":
                        counts["started"] += 1
                results.append(result)
                counts["held"] += result["status"] not in {"succeeded", "planned"}
            return results
        except Exception:
            self.journal.event("change.cycle", "change", {"phase": "failed", **counts})
            return results
        finally:
            counts.update(model_calls=self.model_calls, elapsed_s=self.clock() - started)
            self.journal.event("change.cycle", "change", {"phase": "completed", **counts})

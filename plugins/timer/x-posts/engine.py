"""Collection, worker drafts, gates and one-at-a-time X publication (S-23)."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from axcommon import timeutil
from axcommon.ids import new_ulid
from core.intake.dest.b16b.common import TargetRegistry
from core.intake.dest.b16b.delivery import NotSent

from .contracts import Control, DraftWorker, Events, Inspector, LostReporter, Target
from .environment import XEnvironment
from .journal import XJournal
from .store import QueueStore, restore_draft


class _CurrentRegistry:
    def __init__(self, control):
        self.control = control

    def contains(self, target, operation):
        try:
            return TargetRegistry(list(self.control.targets())).contains(target, operation)
        except Exception:
            return False


class XPosts:
    def __init__(self, directory, *, control: Control, environment: XEnvironment,
                 worker: DraftWorker, inspector: Inspector, events: Events,
                 lost: LostReporter):
        self.store = QueueStore(directory)
        self.control, self.environment = control, environment
        self.worker, self.inspector, self.events = worker, inspector, events
        self.started_at = timeutil.now_ts()
        self.registry = _CurrentRegistry(control)
        self.journal = XJournal(Path(directory) / "delivery.sqlite3", self.registry,
                                emit=events.emit, lost=lost)

    def close(self):
        self.journal.close()

    def recover(self) -> bool:
        """Call at startup; a live sender holds the same interprocess lock."""
        with self.store.single_run() as acquired:
            if not acquired:
                return False
            self.journal.recover()
            self.journal.report_unknown()
            return True

    def _emit(self, kind, subject, data):
        try:
            self.events.emit(kind, subject, data)
        except Exception:
            # Queue and journal remain the source of truth. EventSink supplies its outbox.
            pass

    def _mode(self, target: Target) -> str:
        try:
            value = self.control.effective(target.plugin_id)
            # B3 effective() exposes mode as a scalar.
            return value.get("mode", "collect_only")
        except Exception:
            return "collect_only"

    def inventory(self) -> list[dict]:
        targets = list(self.control.targets())
        TargetRegistry(targets)
        self._emit("automation.targets", "x-posts", {"targets": targets})
        return targets

    def collect(self, target: Target, period: str, *, scheduled_at: str,
                queries: tuple[str, ...] = (), missed: bool = False, generate: bool = True) -> dict:
        run_id = new_ulid()
        timeutil.parse_ts(scheduled_at)
        if missed:
            return self._run_result(run_id, target, scheduled_at, "missed", "schedule_missed", [])
        if self._mode(target) not in ("run", "collect_only"):
            return self._run_result(run_id, target, scheduled_at, "skipped", "mode_blocked", [])
        try:
            self.inventory()
            stats = self.environment.collect("x-stats-" + run_id.lower())
            observation = self.store.observation(target, period, timeutil.now_ts(),
                                                 {"statistics": dict(stats.output)})
        except Exception:
            return self._run_result(run_id, target, scheduled_at, "failed", "collection_failed", [])
        try:
            materials = [self.environment.collect("x-search-" + new_ulid().lower(), query=query).output
                         for query in queries]
        except Exception:
            return self._run_result(run_id, target, scheduled_at, "failed", "collection_failed", [observation])
        if materials:
            try:
                observation = self.store.observation(target, period, timeutil.now_ts(),
                                                     {"statistics": dict(stats.output), "materials": materials})
            except Exception:
                return self._run_result(run_id, target, scheduled_at, "failed", "collection_save_failed", [observation])
        # Re-read after collection: control may have changed while an env job ran.
        if self._mode(target) != "run" or not generate:
            return self._run_result(run_id, target, scheduled_at, "collected", "collect_only" if self._mode(target) != "run" else "", [observation])
        return self.draft(target, observation, scheduled_at=scheduled_at, run_id=run_id)

    def draft(self, target: Target, observation: str, *, scheduled_at: str, run_id: str | None = None) -> dict:
        run_id = run_id or new_ulid()
        saved = self.store.read_observation(observation)
        if (self._mode(target) != "run" or saved is None
                or saved["target"] != target.delivery_target()
                or timeutil.parse_ts(saved["collected_at"]) < timeutil.parse_ts(self.started_at)):
            return self._run_result(run_id, target, scheduled_at, "skipped", "draft_not_eligible", [])
        if not self.registry.contains(target.delivery_target(), "投稿"):
            return self._run_result(run_id, target, scheduled_at, "failed", "target_not_listed", [observation])
        try:
            proposals = self.worker.generate(target, saved["period"], saved["payload"]["statistics"],
                                             saved["payload"].get("materials", []))
        except Exception:
            return self._run_result(run_id, target, scheduled_at, "failed", "generation_failed", [observation])
        identifiers = []
        for draft in proposals:
            # Switching back to collect_only also stops a partial batch of proposals.
            if self._mode(target) != "run":
                break
            try:
                XEnvironment.artifacts(draft)
                try:
                    outcome = self.inspector.inspect(target, draft, "draft").outcome
                except Exception:
                    outcome = "error"
                status = {"passed": "ready", "rejected": "rejected", "error": "check_error"}[outcome]
                reason = "" if outcome == "passed" else "inspection_" + outcome
                candidate = self.store.add(target, saved["period"], draft, timeutil.now_ts(), status, reason)
                identifiers.append(candidate["id"])
            except Exception:
                held_id = self.store.reject(target, saved["period"], draft, timeutil.now_ts())
                self._emit("automation.draft_failed", held_id, {"reason": "invalid_proposal"})
        return self._run_result(run_id, target, scheduled_at, "drafted", "", [observation, *identifiers])

    def _run_result(self, run_id, target, scheduled_at, result, reason, input_ids):
        data = {"run_id": run_id, "process_id": target.plugin_id, "scheduled_at": scheduled_at,
                "input_ids": input_ids, "result": result, "reason": reason}
        self._emit("automation.run", target.plugin_id, data)
        return data

    @staticmethod
    def _matches(row, target):
        return row is not None and all(row[key] == getattr(target, key) for key in
                                       ("plugin_id", "project", "repo", "destination"))

    @staticmethod
    def _job_id(row):
        return row["id"].lower() + "-" + str(row["failures"] + 1)

    def _sync(self, row, record):
        if not record or record.get("state") not in ("sent", "failed", "unknown", "launching"):
            return row
        state = record["state"]
        fields = {"status": "held" if state == "failed" and row["failures"] >= 3 else state,
                  "reason": record["reason"], "external_id": record["external_id"]}
        if state == "sent":
            fields["sent_at"] = row["sent_at"] or record["checked_at"]
        self.store.update(row["id"], **fields)
        return self.store.get(row["id"])

    def send_one(self, target: Target, *, candidate_id: str | None = None,
                 missed: bool = False, before_post=None) -> dict:
        if missed:
            return {"state": "skipped", "reason": "schedule_missed"}
        with self.store.single_run() as acquired:
            if not acquired:
                return {"state": "skipped", "reason": "sender_busy"}
            mode = self._mode(target)
            if mode != "run":
                return {"state": "skipped", "reason": "collect_only" if mode == "collect_only" else "mode_blocked"}
            if not self.registry.contains(target.delivery_target(), "投稿"):
                self._emit("permission.checked", target.repo, {"operation": "投稿", "result": "denied"})
                return {"state": "denied", "reason": "target_not_listed"}
            if candidate_id is not None:
                row = self.store.get(candidate_id)
                if not self._matches(row, target):
                    return {"state": "denied", "reason": "candidate_not_registered"}
                rows = [row]
            else:
                rows = self.store.candidates(target)
            now = timeutil.now()
            eligible = []
            for row in rows:
                record = self.journal.get(target.plugin_id, target.destination, row["id"])
                row = self._sync(row, record)
                if row["failures"] >= 3 or row["status"] not in ("ready", "failed"):
                    continue
                if timeutil.parse_ts(row["due_at"]) > now:
                    continue
                if timeutil.parse_ts(row["created_at"]) < timeutil.parse_ts(self.started_at) and not record:
                    continue
                eligible.append(row)
            if not eligible:
                return {"state": "skipped", "reason": "no_eligible_candidate"}
            # The queue is shared by destinations; an unresolved send still consumes its spacing window.
            latest = self.store.latest_send(target.destination)
            activity = [timeutil.parse_ts(latest)] if latest else []
            activity.extend(timeutil.parse_ts(r["last_attempt"]) for r in self.store.candidates(target)
                            if r["status"] in ("launching", "unknown") and r["last_attempt"])
            if activity and now - max(activity) < timedelta(hours=3):
                return {"state": "skipped", "reason": "minimum_spacing"}
            row = min(eligible, key=lambda r: (timeutil.parse_ts(r["due_at"]), r["created_at"], r["id"]))
            draft = restore_draft(row)

            def preflight():
                try:
                    outcome = self.inspector.inspect(target, draft, "send").outcome
                except Exception:
                    outcome = "error"
                if outcome != "passed":
                    self.store.update(row["id"], status="rejected" if outcome == "rejected" else "check_error",
                                      reason="inspection_" + outcome)
                    return False
                # Gate evaluation can take time. Fetch effective mode and permission again at its end.
                return self._mode(target) == "run" and self.control.check(target.delivery_target(), "投稿") is True

            def send():
                if self._mode(target) != "run" or self.control.check(target.delivery_target(), "投稿") is not True:
                    raise NotSent()
                self.store.update(row["id"], status="launching", last_attempt=timeutil.now_ts())
                if before_post is not None:
                    before_post(row["id"])
                if self._mode(target) != "run" or self.control.check(target.delivery_target(), "投稿") is not True:
                    raise NotSent()
                result = self.environment.post(self._job_id(row), draft)
                if result.state != "succeeded":
                    if result.definitely_not_sent:
                        raise NotSent()
                    raise RuntimeError("x_send_outcome_unknown")
                return self.environment.receipt(target, result.output)

            result = self.journal.deliver(
                mode=mode, target=target.delivery_target(), operation="投稿", source_item_id=row["id"],
                registered=True, expected_destination=target.destination, permission_reader=self.control,
                preflight=preflight, send=send)
            if result.get("state") == "denied" and result.get("reason") == "permission_denied":
                self._emit("permission.checked", target.repo, {"operation": "投稿", "result": "denied"})
            self._sync(self.store.get(row["id"]), result)
            if result.get("state") == "failed":
                failures = row["failures"] + 1
                self.store.update(row["id"], failures=failures, status="held" if failures >= 3 else "failed")
                if failures >= 3:
                    self._emit("automation.retry_exhausted", row["id"], {"reason": "three_confirmed_failures"})
            return result

    def reconcile(self, target: Target, candidate_id: str) -> dict:
        """Lookup only: this entry never initiates a post or treats absence as success."""
        with self.store.single_run() as acquired:
            if not acquired:
                return {"state": "skipped", "reason": "sender_busy"}
            row = self.store.get(candidate_id)
            if not self._matches(row, target):
                return {"state": "denied", "reason": "candidate_not_registered"}
            key = (target.plugin_id, target.destination, candidate_id)
            record = self.journal.get(*key)
            if not record or record["state"] != "unknown":
                return record or {"state": "skipped", "reason": "not_unknown"}
            lookup = getattr(self.environment.runner, "lookup", None)
            if lookup is None:
                raise NotImplementedError("B14b2 receipt lookup binding is required")
            try:
                result = lookup(self._job_id(row))
            except Exception:
                return record
            external_id = self.environment.receipt(target, result.output) if result.state == "succeeded" else None
            result = self.journal.confirm(key, external_id=external_id,
                                          definitely_not_sent=result.definitely_not_sent)
            self._sync(row, result)
            if result["state"] == "failed":
                failures = row["failures"] + 1
                self.store.update(candidate_id, failures=failures, status="held" if failures >= 3 else "failed")
            return result

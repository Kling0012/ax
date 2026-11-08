"""Adapters to the actual b14-1/2/3 Python boundaries."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4

from .ports import DependencyUnavailable, Ports


class CollectionAdapter:
    def __init__(self, service, journal, *, item_builder, owner="change", lease_seconds=9000, budgets=None):
        self.service, self.store, self.registry = service, service.store, service.registry
        self.journal, self.item_builder, self.owner = journal, item_builder, owner
        self.lease_seconds = lease_seconds
        self.budgets = budgets or {}

    def collect(self, *, dry_run: bool, mode: str) -> list[dict]:
        from ..collection import ItemKey

        cycle = "change-" + uuid4().hex
        self.store.begin_cycle(cycle, mode=mode, dry_run=dry_run, **self.budgets)
        # Reconcile before any new claim, including after process restart.
        self.service.reconcile_external()
        self.service.collect(cycle)
        results = []
        if dry_run or mode != "run":
            rows = self.store.waiting(self.registry)
        else:
            rows = []
            while True:
                claim = self.service.claim_next(cycle, self.owner, lease_seconds=self.lease_seconds)
                if claim is None:
                    break
                self.journal.put("collection_claim", claim["key"], claim)
                rows.append(claim)
        for row in rows:
            key = ItemKey.decode(row["key"])
            target = self.registry.resolve(key)
            if target is None:
                continue
            try:
                result = self.item_builder(deepcopy(row), key, target)
                # The builder may enrich trusted provider data, but cannot move
                # an item to a different provider/repository/number.
                if any(result.get(k) != v for k, v in key.as_dict().items()):
                    raise DependencyUnavailable("collection_identity_mismatch")
                if result.get("implementation_repo", "").lower() != target.implementation_repo:
                    raise DependencyUnavailable("collection_mapping_mismatch")
                results.append(result)
            except Exception:
                if mode == "run" and not dry_run:
                    self.store.finish(row, "ejected", reason="item_adapter_unavailable")
                    self.store.hold(key, "parked", reason="item_adapter_unavailable", check_at=self._check_at(60))
        return results

    def _check_at(self, delay):
        return datetime.fromtimestamp(self.store.clock() + delay, timezone.utc).isoformat()

    def checkpoint(self, item, state, reason):
        from ..collection import ItemKey
        from .flow import item_key

        key = ItemKey(**{k: item[k] for k in ("provider", "issue_repo", "kind", "number")})
        claim = self.journal.get("collection_claim", key.encode())
        if claim is None:
            if state in {"queued", "parked"}:
                return
            raise DependencyUnavailable("collection_claim_missing")
        record = self.store.get(key)
        if state == "succeeded" and record and record["state"] == "completed":
            return
        try:
            self.store.validate_lease(claim)
            if state == "running":
                self.store.renew(claim, lease_seconds=self.lease_seconds)
                self.store.record_submission(claim, item.get("item_id") or item_key(item))
                return
            if state == "queued":
                return
            self.store.finish(claim, "completed" if state == "succeeded" else "ejected", reason=reason)
            if state != "succeeded":
                hold = {"waiting_user": "waiting_user", "retry_wait": "retry"}.get(state, "parked")
                if hold == "retry":
                    # Retry has already passed the originating route policy.
                    # Unknown external operations are parked, never reopened.
                    self.store.reopen(key, authorized=True)
                self.store.hold(key, hold, reason=reason, check_at=self._check_at(60))
        except (ValueError, KeyError) as exc:
            raise DependencyUnavailable("collection_lease_stale") from exc


from ..safety.adapter import SafetyAdapter


class VerificationAdapter:
    def __init__(self, runner, *, scrub, request_builder, cache=None, recipes=None):
        self.runner, self.scrub, self.request_builder = runner, scrub, request_builder
        self.cache, self.recipes = cache, recipes

    def verify(self, item, target, *, head_sha, base_sha):
        from ..verification import run_verification
        request = self.request_builder(item, target)
        # Trust, mapping and revisions are taken from the registered target and
        # fresh GitHub snapshot, never accepted from a PR-authored request.
        request.update(repo=target["implementation_repo"], head_sha=head_sha, base_sha=base_sha,
                       untrusted=item.get("author") not in target["trusted_authors"], test_recipe=target["test_recipe"],
                       untrusted_forbidden_paths=target["untrusted_forbidden_paths"], outbound_allowlist=target["outbound_allowlist"])
        return run_verification(request, self.runner, scrub=self.scrub, cache=self.cache, recipes=self.recipes)


def create_ports(*, collection_service, journal, item_builder, safety_gate, verification_runner,
                 verification_request, work, github, permissions, notifications, publications,
                 release, outbound_check, scrub, budgets=None) -> Ports:
    """Trusted deployment factories can call this without inventing wire APIs."""
    return Ports(
        CollectionAdapter(collection_service, journal, item_builder=item_builder, budgets=budgets),
        SafetyAdapter(safety_gate, sensitivity_of=lambda item: "private"),
        VerificationAdapter(verification_runner, scrub=scrub, request_builder=verification_request),
        work, github, permissions, notifications, publications, release, outbound_check, scrub,
    )

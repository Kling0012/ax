"""Pure, fail-closed merge gate (S-19.3 through S-19.7)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
EVIDENCE_KEYS = {
    "repo", "base_sha", "head_sha", "recipe_version", "command", "validation_kind",
    "attempt", "started_at", "ended_at", "exit_code", "timed_out", "complete",
    "output_tail", "test_names", "environment",
}


def full_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA.fullmatch(value))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    route: str = "parked"
    next_check: float | None = None


def independent_safe(receipt: dict, *, head: str, revision: str) -> bool:
    if not isinstance(receipt, dict) or receipt.get("status") != "passed":
        return False
    if receipt.get("head_sha") != head or receipt.get("input_revision") != revision:
        return False
    reviews = receipt.get("reviews")
    if not isinstance(reviews, list) or len(reviews) != 2:
        return False
    reviewers = set()
    for review in reviews:
        if not isinstance(review, dict):
            return False
        if not isinstance(review.get("reviewer"), str) or not review["reviewer"]:
            return False
        reviewers.add(review["reviewer"])
        if review.get("verdict") != "safe" or not isinstance(review.get("summary"), str):
            return False
        concerns = review.get("concerns")
        if not isinstance(concerns, list) or any(c != "quality" for c in concerns):
            return False
    return len(reviewers) == 2


def valid_verification(receipt: dict, *, repo: str, base: str, head: str, untrusted: bool) -> bool:
    from ..verification.evidence import compare, complete

    if not isinstance(receipt, dict) or receipt.get("status") != "passed":
        return False
    evidence = receipt.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    groups: dict[tuple, list] = {}
    baselines: dict[tuple, dict] = {}
    executions = set()
    for e in evidence:
        if not isinstance(e, dict) or not EVIDENCE_KEYS.issubset(e) or not complete(e):
            return False
        if e["repo"].lower() != repo.lower() or e["base_sha"] != base or e["head_sha"] not in {base, head}:
            return False
        if e.get("execution_id") in executions:
            return False
        executions.add(e.get("execution_id"))
        if str(e["validation_kind"]).startswith("dependency-"):
            if e["exit_code"] != 0:
                return False
            continue
        key = (e["recipe_version"], e["command"], e["validation_kind"], e["environment"])
        if e["head_sha"] == base and e.get("revision_role") == "base":
            if key in baselines:
                return False
            baselines[key] = e
        elif e["head_sha"] == head:
            groups.setdefault(key, []).append(e)
        else:
            return False
    if not groups:
        return False
    if untrusted and (not any(k[2] in {"all", "full"} for k in groups) or set(groups) != set(baselines)):
        return False
    for key, records in groups.items():
        if untrusted:
            if len(records) != 2 or len({e["attempt"] for e in records}) != 2:
                return False
            if any(compare(baselines[key], e)["accepted"] is not True for e in records):
                return False
        elif any(e["exit_code"] != 0 for e in records):
            return False
    return True


def requirements_complete(requirements: Any, expected_ids: Any) -> bool:
    if not isinstance(requirements, list) or not isinstance(expected_ids, list) or not expected_ids:
        return False
    if any(not isinstance(i, str) or not i for i in expected_ids) or len(set(expected_ids)) != len(expected_ids):
        return False
    if any(not isinstance(r, dict) for r in requirements):
        return False
    ids = [r.get("id") for r in requirements]
    if len(ids) != len(set(ids)) or set(ids) != set(expected_ids):
        return False
    return all(r.get("status") == "done" and r.get("source") and r.get("text") and r.get("evidence") for r in requirements)


class MergeGate:
    def evaluate(self, item: dict, target: dict, snapshot: dict, safety: dict,
                 verification: dict, permission: dict, *, now: float,
                 instruction: dict | None = None, notification: dict | None = None) -> Decision:
        repo, pr = item["implementation_repo"], item["pr"]
        head, base = snapshot.get("head_sha"), snapshot.get("base_sha")
        if target.get("implementation_repo", "").lower() != repo.lower():
            return Decision(False, "target_mismatch")
        if snapshot.get("repo", "").lower() != repo.lower() or snapshot.get("pr") != pr:
            return Decision(False, "pr_mismatch")
        if not full_sha(head) or not full_sha(base):
            return Decision(False, "incomplete_sha")
        if snapshot.get("comments_complete") is not True or snapshot.get("unprocessed_comments") != []:
            return Decision(False, "comments_pending", "review")
        if not isinstance(snapshot.get("input_revision"), str) or not snapshot["input_revision"]:
            return Decision(False, "input_revision_missing", "review")
        if not independent_safe(safety, head=head, revision=snapshot["input_revision"]):
            return Decision(False, "safety_stale", "review")
        untrusted = item.get("author") not in target.get("trusted_authors", [])
        if not valid_verification(verification, repo=repo, base=base, head=head, untrusted=untrusted):
            return Decision(False, "verification_stale", "verify")
        if not requirements_complete(snapshot.get("requirements"), snapshot.get("required_ids")):
            return Decision(False, "requirements_incomplete", "fix")
        if snapshot.get("requirements_revision") != snapshot["input_revision"]:
            return Decision(False, "requirements_stale", "review")
        if snapshot.get("mergeable") is not True:
            return Decision(False, "merge_conflict", "resolve_conflict")
        if snapshot.get("state") != "open" or snapshot.get("draft") is not False:
            return Decision(False, "pr_not_ready")
        if snapshot.get("publication_head") != head or snapshot.get("outbound_checked") is not True:
            return Decision(False, "publication_stale", "publish")
        sources, runs = target.get("ci_sources"), snapshot.get("ci")
        if not isinstance(sources, list) or not isinstance(runs, list) or snapshot.get("ci_complete") is not True:
            return Decision(False, "ci_unavailable")
        for source in sources:
            if not isinstance(source, str) or not source:
                return Decision(False, "ci_configuration_invalid")
            matching = [r for r in runs if r.get("source") == source]
            if not matching:
                return Decision(False, "ci_missing")
            # Adapter supplies the latest check per configured source. Ambiguity
            # fails closed instead of cherry-picking an older successful run.
            if len(matching) != 1:
                return Decision(False, "ci_ambiguous")
            run = matching[0]
            if run.get("head_sha") != head or run.get("status") != "success" or run.get("origin") != "configured":
                return Decision(False, "ci_mismatch")
        if target.get("merge_method") not in {"merge", "squash", "rebase"}:
            return Decision(False, "merge_method_missing")
        if snapshot.get("can_merge") is not True:
            return Decision(False, "github_permission_missing")
        scoped = (instruction or {}).get("repo", "").lower() == repo.lower() and (instruction or {}).get("pr") == pr
        instructed = bool(scoped and instruction.get("head_sha") == head and instruction.get("operation") == "merge"
                          and instruction.get("source") == "user" and instruction.get("card_id")
                          and instruction.get("instruction_ref") and instruction.get("active") is True)
        if permission.get("allowed") is not True and not instructed:
            return Decision(False, "permission_missing", "waiting_user")
        if untrusted:
            if not instructed:
                return Decision(False, "untrusted_instruction_required", "waiting_user")
            if snapshot.get("collect_only_reviewed_head") != head:
                return Decision(False, "collect_only_required")
            if not notification or not (notification.get("accepted") is True or notification.get("status") == "delivered"):
                return Decision(False, "merge_notice_required", "notify")
            if notification.get("head_sha") != head or notification.get("instruction_ref") != instruction["instruction_ref"]:
                return Decision(False, "merge_notice_stale", "notify")
            import math
            accepted_at = (notification.get("accepted_at") if notification.get("accepted") is True
                           else notification.get("delivered_at"))
            if type(accepted_at) not in {int, float} or not math.isfinite(accepted_at) or accepted_at < 0 or accepted_at > now:
                return Decision(False, "merge_notice_unconfirmed", "notify")
            if now < accepted_at + 1800:
                return Decision(False, "merge_notice_wait", "retry_wait", accepted_at + 1800)
        return Decision(True, "ready", "merge")

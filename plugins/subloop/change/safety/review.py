"""Independent tool-free reviews. Model selection/execution is injected by S-8."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Callable

from axcommon.defaults import Defaults
from axcommon.policy import allowed
from axcommon.schemas import load
from jsonschema import Draft202012Validator
from .input import PreparedInput, sanitize

SCHEMA = "ext/b14/safety/review"


@dataclass(frozen=True)
class Candidate:
    id: str
    resource: dict
    call: Callable[[dict], object]

    def __post_init__(self):
        if not self.id or self.resource.get("id") != self.id or self.resource.get("kind") != "model":
            raise ValueError("candidate identity must match the registered model")


def _complete(value, fragment):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    schema = load(SCHEMA)
    for part in fragment.strip("/").split("/"):
        schema = schema[part]
    return value if Draft202012Validator(schema).is_valid(value) else None


def _ask(candidates, payload, *, purpose, sensitivity, fragment, exclude=()):
    for candidate in candidates:
        if candidate.id in exclude or not allowed(sensitivity, candidate.resource):
            continue
        request = {"purpose": purpose, "input": deepcopy(payload), "tools": [],
                   "sensitivity": sensitivity, "independent": True}
        try:
            value = candidate.call(request)
        except Exception:
            # Timeout/runtime failure is an item-local stop, never a safe verdict.
            return None, candidate.id
        parsed = _complete(value, fragment)
        if parsed is not None:
            return parsed, candidate.id
        # Incomplete schema: try the next eligible S-8 candidate.
    return None, None


def preclassify(prepared: PreparedInput, candidates, *, sensitivity: str) -> dict:
    if prepared.state != "ready":
        return {"action": "parked", "reason": prepared.reason}
    payload = {"body": prepared.body, "diff": prepared.diff}
    outcomes = []
    for _ in range(2):
        result, _model = _ask(candidates, payload, purpose="change_preclassification", sensitivity=sensitivity,
                              fragment="/$defs/classification")
        outcomes.append(result["classification"] if result else None)
    close = outcomes[0] in {"spam", "unrelated"} and outcomes[0] == outcomes[1]
    return {"action": "close_candidate" if close else "safety_review", "outcomes": outcomes}


def neutral_summary(summary: str) -> bool:
    """Mechanical residual check; semantic inspection is still mandatory."""
    if not summary.strip() or sanitize(summary)[1]:
        return False
    patterns = (
        r"\b[a-z][a-z0-9+.-]*://|\bwww\.",
        r"`|\$\(|(?:^|\n)\s*(?:\$|>)\s",
        r"\b(?:sudo|curl|wget|bash|powershell|eval|exec)\b",
        r"\b(?:ignore|override|disregard)\b.{0,60}\b(?:instructions?|rules?|system|previous)\b",
        r"\b(?:run|execute)\s+(?:the\s+)?(?:command|script|shell)\b",
        r"(?:指示|命令|ルール).{0,12}(?:無視|上書き)|(?:実行|送信|削除)して(?:ください|下さい)",
    )
    return not any(re.search(pattern, summary, flags=re.I) for pattern in patterns)


class SafetyGate:
    def __init__(self, reviewers, *, injection_check: Callable[[dict], bool], max_bytes=None):
        if len(reviewers) != 2:
            raise ValueError("two independent reviewer lanes required")
        self.reviewers = reviewers
        self.injection_check = injection_check
        self.max_bytes = Defaults.load().get("change_input_max_bytes") if max_bytes is None else max_bytes
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise ValueError("positive byte limit required")

    def review(self, prepared: PreparedInput, *, sensitivity: str, kind: str = "issue") -> dict:
        if kind not in {"issue", "pr", "comment"}:
            raise ValueError("invalid input kind")
        stopped = {"state": "parked", "reason": "safety_unknown", "worker_input": None,
                   "reviews": [], "reviewer_ids": [], "quality": [], "input_removed": prepared.removed}
        if prepared.state != "ready":
            return {**stopped, "reason": prepared.reason}
        payload = {"kind": kind, "body": prepared.body, "diff": prepared.diff}
        results, identities = [], []
        for lane in self.reviewers:
            result, identity = _ask(lane, payload, purpose="change_safety", sensitivity=sensitivity,
                                    fragment="/$defs/verdict", exclude=identities)
            results.append(result)
            identities.append(identity)
        stopped.update(reviews=results, reviewer_ids=identities)
        if any(r is None or r["verdict"] != "safe" or
               set(r["concerns"]) & {"malicious", "security"} for r in results):
            return stopped
        worker_input = {"summaries": [r["summary"] for r in results]}
        if len(json.dumps(worker_input, ensure_ascii=False).encode("utf-8")) > self.max_bytes:
            return {**stopped, "reason": "summary_too_large"}
        if not all(neutral_summary(summary) for summary in worker_input["summaries"]):
            return {**stopped, "reason": "summary_injection"}
        try:
            # Inspect each independently generated summary; no original text escapes.
            checks = [self.injection_check({"summary": summary}) is True for summary in worker_input["summaries"]]
        except Exception:
            return {**stopped, "reason": "summary_inspection_unavailable"}
        if not all(checks):
            return {**stopped, "reason": "summary_injection"}
        return {"state": "ready", "reason": None, "worker_input": worker_input,
                "reviews": results, "reviewer_ids": identities,
                "quality": [r["summary"] for r in results if "quality" in r["concerns"]],
                "input_removed": prepared.removed}


def close_plan(item: dict, classification: dict, *, permission_check, outbound_check,
               duplicate: dict | None = None, dry_run=True, collect_only=True) -> dict:
    """Return a plan only. The integration node owns all GitHub operations."""
    reason = None
    if classification.get("action") == "close_candidate":
        outcomes = classification.get("outcomes", [])
        if len(outcomes) == 2 and outcomes[0] in {"spam", "unrelated"} and outcomes[0] == outcomes[1]:
            reason = outcomes[0]
    if duplicate:
        if (duplicate.get("repo") != item["issue_repo"] or duplicate.get("number") == item["number"]
                or type(duplicate.get("number")) is not int or duplicate["number"] < 1 or not duplicate.get("reason")):
            return {"state": "parked", "reason": "invalid_duplicate", "execute": False}
        reason = "duplicate"
    if not reason:
        return {"state": "parked", "reason": "close_unconfirmed", "execute": False}
    plan = {"repo": item["issue_repo"], "number": item["number"], "operation": "close",
            "reason": reason, "duplicate": deepcopy(duplicate), "execute": False}
    try:
        if permission_check(deepcopy(plan)) is not True:
            return {**plan, "state": "waiting_user", "reason": "permission_required"}
        if outbound_check(deepcopy(plan)) is not True:
            return {**plan, "state": "parked", "reason": "outbound_rejected"}
    except Exception:
        return {**plan, "state": "parked", "reason": "close_checks_unavailable"}
    return {**plan, "state": "planned" if dry_run or collect_only else "ready", "dry_run": dry_run,
            "collect_only": collect_only}

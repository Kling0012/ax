"""Complete evidence only; base comparisons never excuse infrastructure errors."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re

from .policy import GateError

FIELDS = ("repo", "base_sha", "head_sha", "recipe_version", "command", "validation_kind", "attempt",
          "started_at", "ended_at", "exit_code", "timed_out", "complete", "output_tail", "test_names", "environment")
KEY = ("head_sha", "recipe_version", "command", "validation_kind", "attempt")


def complete(e):
    if not isinstance(e, dict) or any(k not in e for k in FIELDS):
        return False
    if any(not isinstance(e[k], str) or not e[k] for k in ("repo", "base_sha", "head_sha", "recipe_version", "command", "validation_kind", "attempt", "environment")):
        return False
    if any(not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", e[k]) for k in ("base_sha", "head_sha")):
        return False
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", e["repo"]):
        return False
    if not re.fullmatch(r"[a-z0-9-]{1,40}", e["attempt"]) or e.get("revision_role") not in ("base", "head"):
        return False
    if not isinstance(e.get("execution_id"), str):
        return False
    if e.get("complete") is not True or e.get("executed") is not True or e.get("timed_out") is not False or e.get("output_truncated") is not False:
        return False
    if type(e.get("exit_code")) is not int or not e.get("execution_id"):
        return False
    if not isinstance(e["output_tail"], str) or not isinstance(e.get("failure_evidence"), dict):
        return False
    names = e["test_names"]
    if names is not None and (not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names) or len(names) != len(set(names))):
        return False
    failures = e["failure_evidence"]
    if any(not isinstance(n, str) or not n or not isinstance(d, str) or not d for n, d in failures.items()):
        return False
    if names is not None and not set(failures).issubset(names):
        return False
    if e["exit_code"] == 0 and failures:
        return False
    try:
        start = datetime.fromisoformat(e["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(e["ended_at"].replace("Z", "+00:00"))
        return start.tzinfo is not None and end.tzinfo is not None and start <= end
    except (ValueError, TypeError, AttributeError):
        return False


def make_evidence(request, invocation, result, scrub):
    if not isinstance(result, dict):
        raise GateError("evidence_incomplete")
    if result.get("job_id") != invocation["job_id"] or result.get("sha") != invocation["subject_sha"]:
        raise GateError("result_identity_mismatch")
    e = {k: result.get(k) for k in FIELDS}
    e.update({"repo": request["repo"], "base_sha": request["base_sha"], "head_sha": invocation["subject_sha"],
              "recipe_version": invocation["recipe_version"], "command": invocation["command"],
              "validation_kind": invocation["validation_kind"], "attempt": invocation["attempt"],
              "environment": invocation["environment"], "execution_id": result.get("job_id"),
              "executed": result.get("executed") is True, "output_truncated": result.get("output_truncated") is not False,
              "failure_evidence": result.get("failure_evidence", {}), "revision_role": invocation["revision_role"]})
    # Scrub the entire record, not only stdout: names and diagnostics can contain secrets too.
    clean = scrub(deepcopy(e))
    if not isinstance(clean, dict) or any(clean.get(k) != e[k] for k in ("repo", "base_sha", "head_sha", "recipe_version", "command", "validation_kind", "attempt", "environment", "execution_id", "revision_role")):
        raise GateError("evidence_redaction_failed")
    # If redaction removed comparison material, it cannot prove equivalence.
    clean["diagnostics_redacted"] = clean.get("failure_evidence") != e["failure_evidence"] or clean.get("test_names") != e["test_names"]
    if clean.get("test_names") != e["test_names"]:
        clean["complete"] = False
    if not complete(clean):
        clean["complete"] = False
    return clean


def compare(base, head):
    decision = {"base_execution": base.get("execution_id"), "head_execution": head.get("execution_id"), "accepted": False, "reason": "evidence_incomplete", "base_derived_failures": []}
    if not complete(base) or not complete(head):
        return decision
    same = ("repo", "base_sha", "recipe_version", "command", "validation_kind", "environment")
    if any(base[k] != head[k] for k in same) or base["head_sha"] != head["base_sha"]:
        decision["reason"] = "base_mismatch"
        return decision
    if base["test_names"] is not None:
        if head["test_names"] is None or not set(base["test_names"]).issubset(head["test_names"]):
            decision["reason"] = "test_names_missing"
            return decision
    if head["exit_code"] == 0:
        decision.update(accepted=True, reason="passed")
        return decision
    # Nonzero with no named diagnostics, changed diagnostics or different exit status is not exempt.
    failures = head["failure_evidence"]
    if (base["exit_code"] == 0 or head["exit_code"] != base["exit_code"] or not failures
            or base.get("diagnostics_redacted") or head.get("diagnostics_redacted")
            or any(base["failure_evidence"].get(name) != detail for name, detail in failures.items())):
        decision["reason"] = "new_failure"
        return decision
    decision.update(accepted=True, reason="base_derived_failure", base_derived_failures=sorted(failures))
    return decision


class EvidenceCache:
    """Caller-owned checkpoint cache. Scope additionally pins repo, base and current policy."""
    def __init__(self):
        self._records = {}

    @staticmethod
    def _key(scope, evidence):
        return (scope, *(evidence[k] for k in KEY))

    def put(self, scope, evidence):
        if complete(evidence):
            self._records[self._key(scope, evidence)] = deepcopy(evidence)

    def get(self, scope, expected):
        record = self._records.get(self._key(scope, expected))
        if not record or not complete(record):
            return None
        if any(record.get(k) != expected.get(k) for k in (*KEY, "repo", "base_sha", "environment", "revision_role")):
            return None
        return deepcopy(record)

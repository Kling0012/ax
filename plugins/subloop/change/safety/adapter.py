"""Adapter for integration.ports.SafetyPort, with explicit version binding."""
from copy import deepcopy

from .input import prepare_input


class SafetyAdapter:
    def __init__(self, gate, *, sensitivity_of, max_bytes=None):
        # sensitivity_of reads trusted target/model policy, not issue prose.
        self.gate = gate
        self.sensitivity_of = sensitivity_of
        self.max_bytes = max_bytes

    def review(self, item: dict) -> dict:
        snapshot = deepcopy(item.get("snapshot", {}))
        revision = snapshot.get("input_revision")
        failed = {"status": "failed", "state": "parked", "reason": "input_revision_missing",
                  "head_sha": snapshot.get("head_sha"), "input_revision": revision,
                  "reviews": [], "worker_input": None}
        if not isinstance(revision, str) or not revision:
            return failed
        if "body" not in snapshot or "diff" not in snapshot:
            return {**failed, "reason": "input_incomplete"}
        prepared = prepare_input(snapshot["body"], snapshot["diff"], max_bytes=self.max_bytes)
        result = self.gate.review(prepared, sensitivity=self.sensitivity_of(item), kind=item.get("kind", "issue"))
        reviews = [{**r, "reviewer": identity} for r, identity in zip(result["reviews"], result["reviewer_ids"]) if r is not None]
        return {**result, "status": "passed" if result["state"] == "ready" else "failed",
                "head_sha": snapshot.get("head_sha"), "input_revision": revision, "reviews": reviews}

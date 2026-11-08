"""Stable requirement records and mechanical merge reconciliation (S-19.4)."""
from __future__ import annotations

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from axcommon.schemas import validate


class RequirementBook:
    def __init__(self, records=()):
        self.records = deepcopy(list(records))
        validate("ext/b14/safety/requirements", self.records)
        if len({r["id"] for r in self.records}) != len(self.records):
            raise ValueError("duplicate requirement ID")

    def update(self, *, source: str, texts: list[str]) -> list[dict]:
        """texts must be extracted from SafetyGate.worker_input, never raw comments.

        Existing records remain until explicitly resolved with evidence. An
        edited or removed request cannot silently disappear from a merge gate.
        """
        if not source or not isinstance(texts, list) or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("nonempty source and requirement texts required")
        occurrences = {}
        known = {r["id"]: r for r in self.records}
        for text in texts:
            text = text.strip()
            occurrences[text] = occurrences.get(text, 0) + 1
            rid = "req-" + uuid5(NAMESPACE_URL, repr((source, text, occurrences[text]))).hex
            if rid not in known:
                record = {"id": rid, "source": source, "text": text, "status": "pending", "evidence": []}
                self.records.append(record)
                known[rid] = record
        return deepcopy(self.records)

    def ingest(self, *, source: str, review: dict, extract) -> dict:
        """Extract only from approved summaries; a failed extraction changes nothing."""
        worker_input = review.get("worker_input")
        if review.get("state") != "ready" or not isinstance(worker_input, dict):
            return {"state": "parked", "reason": "unreviewed_requirements"}
        summaries = worker_input.get("summaries")
        if not isinstance(summaries, list) or len(summaries) != 2 or any(not isinstance(t, str) or not t.strip() for t in summaries):
            return {"state": "parked", "reason": "unreviewed_requirements"}
        try:
            texts = extract(deepcopy(worker_input))
            records = self.update(source=source, texts=texts)
        except Exception:
            return {"state": "parked", "reason": "requirement_extraction_unavailable"}
        return {"state": "ready", "requirements": records}

    def complete(self, requirement_id: str, evidence: list[str]):
        if not isinstance(evidence, list) or not evidence or any(not isinstance(ref, str) or not ref.strip() for ref in evidence):
            raise ValueError("evidence references required")
        for record in self.records:
            if record["id"] == requirement_id:
                record.update(status="done", evidence=list(evidence))
                return
        raise KeyError(requirement_id)

    def reconcile(self, *, expected_ids, evidence_check, comments_current=True) -> dict:
        expected = set(expected_ids)
        records = {r["id"]: r for r in self.records}
        missing = sorted(expected - records.keys())
        # Extra records must also be completed; expected_ids cannot hide them.
        incomplete = [r["id"] for r in self.records if r["status"] != "done" or not r["evidence"]]
        invalid = []
        for r in self.records:
            if r["status"] != "done":
                continue
            try:
                if not r["evidence"] or any(evidence_check(ref) is not True for ref in r["evidence"]):
                    invalid.append(r["id"])
            except Exception:
                invalid.append(r["id"])
        okay = bool(records) and not (missing or incomplete or invalid) and comments_current is True
        return {"state": "ready" if okay else "needs_revision", "merge_allowed": okay,
                "missing": missing, "incomplete": incomplete, "invalid_evidence": invalid,
                "comments_current": comments_current}

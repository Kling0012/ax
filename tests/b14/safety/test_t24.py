import pytest

from plugins.subloop.change.safety import RequirementBook, close_plan, information_deadline, issue_route


def test_t24_duplicate_requires_other_issue_in_same_repo_with_reason():
    item = {"issue_repo": "owner/public", "number": 4}
    for duplicate in [{"repo": "owner/private", "number": 1, "reason": "same behavior"},
                      {"repo": "owner/public", "number": 4, "reason": "same"},
                      {"repo": "owner/public", "number": 1, "reason": ""}]:
        assert close_plan(item, {}, duplicate=duplicate, permission_check=lambda p: True, outbound_check=lambda p: True)["state"] == "parked"
    result = close_plan(item, {}, duplicate={"repo": "owner/public", "number": 1, "reason": "same behavior"},
                        permission_check=lambda p: True, outbound_check=lambda p: True)
    assert result["state"] == "planned" and result["reason"] == "duplicate"


def test_t24_question_unknown_bug_and_ambiguity_paths():
    assert issue_route("question")["route"] == "research_answer"
    assert issue_route("question", research_available=False)["route"] == "answer_unknown"
    assert issue_route("bug")["route"] == "diagnose"
    assert issue_route("bug", information_required=True)["state"] == "waiting_information"
    assert issue_route("ambiguous")["state"] == "ready"


def test_t24_deadline_and_late_safe_answer():
    deadline = "2026-11-07T00:00:00Z"
    assert information_deadline(deadline=deadline, now="2026-11-06T00:00:00Z")["state"] == "waiting_information"
    result = information_deadline(deadline=deadline, now=deadline)
    assert result["state"] == "parked" and result["accept_later_answer"]
    assert information_deadline(deadline=deadline, now=deadline, safe_answer=True)["state"] == "ready"


def test_t24_stable_ids_and_updates_preserve_unresolved_requests():
    book = RequirementBook()
    first = book.update(source="issue:o/r:1", texts=["first", "second"])
    again = book.update(source="issue:o/r:1", texts=["second", "first"])
    assert first == again
    result = book.update(source="comment:o/r:1:2", texts=["new requirement"])
    assert len(result) == 3 and len({r["id"] for r in result}) == 3
    book.update(source="issue:o/r:1", texts=["replacement"])
    assert len(book.records) == 4
    assert {"id", "source", "text", "status", "evidence"} == set(book.records[0])


def test_t24_missing_pending_evidence_and_new_comments_block_merge():
    book = RequirementBook()
    records = book.update(source="issue:o/r:1", texts=["requirement"])
    rid = records[0]["id"]
    assert not book.reconcile(expected_ids=[rid], evidence_check=lambda ref: True)["merge_allowed"]
    with pytest.raises(ValueError):
        book.complete(rid, [])
    book.complete(rid, ["event:verified-at-head"])
    assert book.reconcile(expected_ids=[rid], evidence_check=lambda ref: True)["merge_allowed"]
    assert not book.reconcile(expected_ids=[rid, "missing"], evidence_check=lambda ref: True)["merge_allowed"]
    assert not book.reconcile(expected_ids=[rid], evidence_check=lambda ref: False)["merge_allowed"]
    assert not book.reconcile(expected_ids=[rid], evidence_check=lambda ref: True, comments_current=False)["merge_allowed"]
    book.update(source="comment:o/r:1:2", texts=["new requirement"])
    assert not book.reconcile(expected_ids=[rid], evidence_check=lambda ref: True)["merge_allowed"]


def test_t24_evidence_inspection_failure_is_not_success():
    book = RequirementBook()
    rid = book.update(source="issue:1", texts=["request"])[0]["id"]
    book.complete(rid, ["unavailable"])
    def fail(ref):
        raise TimeoutError()
    assert book.reconcile(expected_ids=[rid], evidence_check=fail)["invalid_evidence"] == [rid]


def test_t24_duplicate_ids_in_restored_checkpoint_rejected():
    book = RequirementBook()
    r = book.update(source="issue:1", texts=["request"])[0]
    with pytest.raises(ValueError):
        RequirementBook([r, r])


def test_t24_extractor_sees_only_safety_approved_summaries():
    book = RequirementBook()
    seen = []
    def extract(value):
        seen.append(value)
        return ["request"]
    denied = book.ingest(source="issue:1", review={"state": "parked", "worker_input": None}, extract=extract)
    assert denied["state"] == "parked" and not seen and not book.records
    approved = {"state": "ready", "worker_input": {"summaries": ["neutral first", "neutral second"]}}
    result = book.ingest(source="issue:1", review=approved, extract=extract)
    assert result["state"] == "ready" and seen == [approved["worker_input"]]
    count = len(book.records)
    result = book.ingest(source="issue:1", review=approved, extract=lambda value: ["good", ""])
    assert result["state"] == "parked" and len(book.records) == count


def test_t24_empty_requirements_cannot_skip_extraction():
    assert not RequirementBook().reconcile(expected_ids=[], evidence_check=lambda ref: True)["merge_allowed"]


def test_owned_schemas_are_valid_draft_202012(repo_root):
    import json
    from jsonschema import Draft202012Validator
    for path in (repo_root / "schemas/ext/b14/safety").glob("*.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text()))

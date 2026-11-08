from __future__ import annotations

import json

import pytest

from axcommon import timeutil
from x_posts import Draft, Inspection


# T-64: statistics, worker drafts, gate outcomes and immutable queue registration.
def test_t64_collect_only_records_statistics_and_materials_without_generation(rig):
    result = rig.engine.collect(rig.target, "day", scheduled_at=timeutil.now_ts(), queries=("#sample",))
    assert result["result"] == "collected"
    assert rig.worker.calls == []
    assert rig.env.posts == []
    assert rig.engine.store.candidates(rig.target) == []
    with rig.engine.store.connection() as db:
        observations = [json.loads(row[0]) for row in db.execute("SELECT payload FROM observations")]
    assert observations == [{"statistics": {"views": 12}},
                            {"statistics": {"views": 12}, "materials": [{"items": []}]}]
    assert any(kind == "automation.targets" for kind, _, _ in rig.events.records)
    assert not any(kind == "automation.delivery" for kind, _, _ in rig.events.records)


def test_t64_only_passed_drafts_enter_queue_and_errors_are_distinct(rig):
    rig.start()
    rig.inspector.by_text[("draft", "rejected")] = Inspection("passed", "rejected", "passed", "passed")
    rig.inspector.by_text[("draft", "error")] = RuntimeError("private diagnostic")
    rows = rig.drafts("accepted", "rejected", "error")
    assert {json.loads(row["payload"])["text"]: row["status"] for row in rows} == {
        "accepted": "ready", "rejected": "rejected", "error": "check_error"}
    assert len(rig.worker.calls) == 1
    assert rig.env.posts == []
    assert rig.engine.send_one(rig.target)["state"] == "sent"
    assert len(rig.env.posts) == 1


def test_t64_repeating_a_draft_does_not_change_registered_content(rig):
    rig.start()
    first = rig.drafts("original")[0]
    later = rig.drafts("replaced")[0]
    assert first["id"] == later["id"]
    assert json.loads(later["payload"])["text"] == "original"
    assert len(rig.engine.store.candidates(rig.target)) == 1


@pytest.mark.parametrize("failure", ["collection", "worker"])
def test_t64_failure_does_not_register_candidates(rig, failure):
    rig.start()
    if failure == "collection":
        rig.env.collection_error = RuntimeError("PRIVATE-SENTINEL")
    else:
        rig.worker.error = RuntimeError("PRIVATE-SENTINEL")
    result = rig.engine.collect(rig.target, "day", scheduled_at=timeutil.now_ts())
    assert result["result"] == "failed"
    assert rig.engine.store.candidates(rig.target) == []
    assert rig.env.posts == []
    assert "PRIVATE-SENTINEL" not in repr(rig.events.records)
    if failure == "worker":
        with rig.engine.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_t64_partial_collection_keeps_successful_statistics(rig):
    rig.start()
    original = rig.env.run

    def fail_search(request, artifacts):
        if request["entry"]["args"].get("op") == "search":
            raise RuntimeError("search unavailable")
        return original(request, artifacts)

    rig.env.run = fail_search
    result = rig.engine.collect(rig.target, "day", scheduled_at=timeutil.now_ts(), queries=("#sample",))
    assert result["reason"] == "collection_failed"
    assert rig.worker.calls == []
    with rig.engine.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


@pytest.mark.parametrize("check", ["public", "secret", "privacy", "yomiyasu"])
def test_t64_each_mandatory_gate_is_fail_closed(rig, check):
    rig.start()
    states = dict.fromkeys(("public", "secret", "privacy", "yomiyasu"), "passed")
    states[check] = "error"
    rig.inspector.by_text[("draft", "text")] = Inspection(**states)
    row = rig.drafts("text")[0]
    assert row["status"] == "check_error"
    assert rig.engine.send_one(rig.target)["state"] == "skipped"
    assert rig.env.posts == []


def test_t64_invalid_proposal_is_saved_separately(rig):
    rig.start()
    rig.worker.proposals = [Draft("bad-file", "text", timeutil.now_ts(), ("../image.png",), {"../image.png": b"demo"})]
    result = rig.engine.collect(rig.target, "day", scheduled_at=timeutil.now_ts())
    assert result["result"] == "drafted"
    assert rig.engine.store.candidates(rig.target) == []
    with rig.engine.store.connection() as db:
        held = dict(db.execute("SELECT * FROM rejected_proposals").fetchone())
    assert held["draft_id"] == "bad-file"
    assert held["reason"] == "invalid_proposal"
    assert not rig.env.posts


def test_t64_media_is_immutable_and_only_supported_filenames_are_sent(rig):
    rig.start()
    rig.worker.proposals = [Draft("media", "demo", timeutil.now_ts(), ("1.mp4",), {"1.mp4": b"demo-video"})]
    rig.engine.collect(rig.target, "day", scheduled_at=timeutil.now_ts())
    assert rig.engine.send_one(rig.target)["state"] == "sent"
    _, request, artifacts = rig.env.posts[0]
    assert request["entry"]["args"] == {"text": "text.txt", "images": ["1.mp4"]}
    assert artifacts == {"text.txt": b"demo", "1.mp4": b"demo-video"}

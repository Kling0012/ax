"""T-29: safe defaults, independent items, durable trace and delivery outbox."""
from copy import deepcopy

import pytest

from plugins.subloop.change.integration import ChangeFlow, Journal
from plugins.subloop.change.integration.delivery import Delivery
from tests.fakes.b14.integration import HEAD, NEXT, item, target


@pytest.mark.parametrize("flag", [None, True, "false", 0])
def test_t29_dry_run_omitted_or_non_boolean_never_executes(harness, flag):
    h = harness
    result = h.flow.process(item(), dry_run=flag, mode="run")
    assert result["status"] == "planned" and result["dry_run"] is True
    assert result["artifacts"]["plan"] and result["artifacts"]["notes"]
    assert not h.github.merges and not h.work.calls and not h.notices.sent and not h.release.sent
    assert not h.collection.checkpoints


def test_t29_collect_only_is_the_default_without_workers(harness):
    h = harness
    result = h.flow.process(item(), dry_run=False)
    assert result["reason"] == "collected"
    assert not h.safety.calls and not h.work.calls and not h.verification.calls and not h.github.merges


def test_t29_stopped_item_does_not_stop_patrol(harness):
    h = harness
    h.collection.items = [item(), item(2)]
    h.flow.stop(item(), "user stopped this item")
    results = h.flow.patrol(dry_run=False, mode="run")
    assert [r["status"] for r in results] == ["parked", "succeeded"]
    cycles = [e["data"] for e in h.journal.events() if e["kind"] == "change.cycle"]
    assert cycles[0]["phase"] == "started" and cycles[-1]["phase"] == "completed"
    assert cycles[-1]["found"] == 2 and cycles[-1]["held"] == 1
    h.flow.resume(item())
    assert h.flow.process(item(), dry_run=False, mode="run")["status"] == "succeeded"


def test_t29_patrol_dry_run_propagates_to_collection(harness):
    h = harness
    h.collection.items = [item()]
    assert h.flow.patrol()[0]["status"] == "planned"
    assert h.collection.calls == [(True, "collect_only")]


def test_t29_target_failure_does_not_stop_next_item(harness):
    h = harness
    wrong = {**item(), "implementation_repo": "owner/wrong"}
    h.collection.items = [wrong, item(2)]
    results = h.flow.patrol(dry_run=False, mode="run")
    assert results[0]["reason"] == "target_unavailable" and results[1]["status"] == "succeeded"


def test_t29_duplicate_mapping_is_parked(harness):
    h = harness
    flow = ChangeFlow(h.ports, h.journal, [target(), target()], clock=h.clock)
    assert flow.process(item(), dry_run=False, mode="run")["reason"] == "target_unavailable"
    assert not h.github.merges


def test_t29_question_finishes_without_tdd_verification_or_merge(harness):
    h = harness
    result = h.flow.process(item(route="question"), dry_run=False, mode="run")
    assert result["reason"] == "question_answered"
    assert [op for op, _ in h.work.calls] == ["answer"]
    assert not h.verification.calls and not h.github.merges and not h.release.sent
    assert h.publications.sent[0][2]["repo"] == "owner/issues"


def test_t29_question_without_comment_permission_waits_on_issue_card(harness):
    h = harness
    h.permissions.allowed = False

    result = h.flow.process(item(route="question"), dry_run=False, mode="run")

    assert result["status"] == "waiting_user" and result["reason"] == "permission_missing"
    assert h.permissions.cards == [{"repo": "owner/issues", "pr": None, "head_sha": HEAD,
                                   "reason": "comment_permission_missing"}]
    assert not h.publications.sent


def test_t29_new_pr_requires_complete_verification_before_publication(harness):
    h = harness
    h.flow.fix_rounds = 0
    proposed = {**item(route="change"), "pr": None}
    h.verification.verify = lambda *_args, **_kwargs: {
        "status": "passed", "reason": "claimed", "evidence": [], "comparisons": [], "unperformed": [],
    }

    result = h.flow.process(proposed, dry_run=False, mode="run")

    assert result["reason"] == "trusted_internal_decision_required"
    assert not h.publications.sent and not h.github.merges and not h.release.sent


def test_t29_new_pr_receipt_does_not_authorize_unaccepted_pr(harness):
    h = harness
    proposed = {**item(route="change"), "pr": None}
    h.publications.on_send = lambda _key, _destination, _body: {
        "status": "delivered", "delivered_at": h.clock(), "repo": "owner/code", "pr": 401,
    }

    result = h.flow.process(proposed, dry_run=False, mode="run")

    assert result["reason"] == "pr_identity_mismatch"
    assert [op for op, _ in h.work.calls] == ["tdd-change", "prepare_publication"]
    assert h.publications.sent[0][2]["repo"] == "owner/code"
    assert h.publications.sent[0][2]["pr"] is None
    assert not h.github.merges and not h.release.sent
    assert h.journal.change_ledger.get("request-1")["state"] == "waiting"


def test_t29_publication_worker_receives_trusted_worktree_reference(harness):
    h = harness
    candidate = {**item(), "workdir_id": "worktree-7", "branch": "ai/change-7"}
    safety = h.safety.review({**candidate, "snapshot": h.github.live})

    h.flow._work(candidate, target(), "prepare_publication", safety=safety)

    worker_item = h.work.calls[0][1]
    assert worker_item["workdir_id"] == "worktree-7" and worker_item["branch"] == "ai/change-7"


def test_t29_learning_candidates_have_tracked_selection_and_consumption(harness):
    h = harness
    h.permissions.allowed = False
    h.flow.process(item(), dry_run=False, mode="run")
    ev = next(e for e in h.journal.events() if e["kind"] == "change.learning")
    candidate = ev["data"]
    assert candidate["status"] == "pending" and candidate["source_event"] and candidate["evidence_ref"]
    h.flow.learning.transition(candidate["id"], "selected", reason="useful")
    h.flow.learning.transition(candidate["id"], "consumed", reason="applied")
    with pytest.raises(ValueError):
        h.flow.learning.transition(candidate["id"], "selected", reason="cannot regress")
    assert h.journal.get("learning", candidate["id"])["status"] == "consumed"
    with pytest.raises(KeyError):
        h.flow.learning.transition("unknown", "selected", reason="not found")


def test_t29_timeline_failure_retains_same_event_ids_for_retry(harness):
    h = harness
    h.flow.process(item(), mode="run")
    before = [e["id"] for e in h.journal.events(pending=True)]
    assert h.journal.flush(lambda ev: False) == 0
    assert [e["id"] for e in h.journal.events(pending=True)] == before
    delivered = []
    assert h.journal.flush(lambda ev: delivered.append(ev["id"])) == len(before)
    assert delivered == before and not h.journal.events(pending=True)


def test_t29_restart_keeps_merge_and_handoff_receipts(harness, tmp_path):
    h = harness
    h.flow.process(item(), dry_run=False, mode="run")
    reopened = Journal(tmp_path / "change.db", scrub=h.ports.scrub, clock=h.clock)
    try:
        reopened.change_ledger = h.journal.change_ledger
        flow = ChangeFlow(h.ports, reopened, [target()], clock=h.clock)
        assert flow.process(item(), dry_run=False, mode="run")["status"] == "succeeded"
        assert len(h.github.merges) == len(h.release.sent) == 1
    finally:
        reopened.close()


def test_t29_unknown_delivery_does_not_resend_without_confirmed_absence(harness):
    h = harness
    h.notices.result = {"status": "unknown"}
    d = Delivery(h.journal, h.notices, outbound_check=lambda b: True, clock=h.clock)
    assert d.deliver("n1", "user", {"notice": 1})["status"] == "unknown"
    h.notices.observed["n1"] = {"status": "unknown"}
    h.clock.advance(120)
    assert d.deliver("n1", "user", {"notice": 1})["status"] == "unknown"
    assert len(h.notices.sent) == 1


def test_t29_outbound_check_failure_has_no_delivery(harness):
    h = harness
    d = Delivery(h.journal, h.notices, outbound_check=lambda b: False, clock=h.clock)
    assert d.deliver("n1", "user", {"notice": 1})["status"] == "blocked"
    assert not h.notices.sent


def test_t29_release_failure_leaves_merged_item_pending(harness):
    h = harness
    h.release.result = {"status": "retry", "next_retry": h.clock() + 200}
    assert h.flow.process(item(), dry_run=False, mode="run")["reason"] == "release_handoff_pending"
    h.clock.advance(200)
    h.release.result = None
    assert h.flow.process(item(), dry_run=False, mode="run")["reason"] == "release_pending"
    assert len(h.github.merges) == 1


def test_t29_model_budget_stops_only_current_item(harness):
    h = harness
    h.flow.model_calls_per_cycle = 1
    result = h.flow.process(item(), dry_run=False, mode="run")
    assert result["status"] == "retry_wait" and not h.safety.calls and not h.work.calls


def test_t29_safety_failure_precedes_bug_investigation(harness):
    h = harness
    h.safety.fail = True
    assert h.flow.process(item(route="bug"), dry_run=False, mode="run")["reason"] == "safety_failed"
    assert not h.work.calls

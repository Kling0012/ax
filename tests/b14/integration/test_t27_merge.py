"""T-27: repair/merge controls with complete evidence and fake GitHub."""
from copy import deepcopy

import pytest

from plugins.subloop.change.integration.gate import MergeGate, valid_verification
from tests.fakes.b14.integration import BASE, HEAD, NEXT, evidence, item, snapshot, target


def receipts(h, it=None):
    it = it or item()
    live = h.github.view(it, target())
    safe = h.safety.review({**it, "snapshot": live})
    verified = h.verification.verify(it, target(), head_sha=live["head_sha"], base_sha=live["base_sha"])
    return live, safe, verified


def test_t27_expected_head_and_registered_method_and_handoff_once(harness):
    h = harness
    result = h.flow.process(item(), dry_run=False, mode="run")
    assert result["reason"] == "release_pending"
    assert h.github.merges == [("owner/code", 1, HEAD, "merge")]
    assert h.release.sent[0][2]["issue"]["issue_repo"] == "owner/issues"
    assert h.flow.process(item(), dry_run=False, mode="run")["status"] == "succeeded"
    assert len(h.github.merges) == len(h.release.sent) == 1


@pytest.mark.parametrize("field,value,reason", [
    ("ci", [], "ci_missing"), ("head_sha", "short", "incomplete_sha"),
    ("mergeable", False, "merge_conflict"), ("can_merge", False, "github_permission_missing"),
    ("ci_complete", False, "ci_unavailable"), ("requirements", [], "requirements_incomplete"),
    ("unprocessed_comments", [{"comment_id": "new"}], "comments_pending"),
    ("comments_complete", False, "comments_pending"), ("publication_head", NEXT, "publication_stale"),
    ("draft", True, "pr_not_ready"), ("state", "closed", "pr_not_ready"),
])
def test_t27_gate_denies_missing_or_changed_inputs(harness, field, value, reason):
    live, safe, verified = receipts(harness)
    live[field] = value
    result = MergeGate().evaluate(item(), target(), live, safe, verified, {"allowed": True}, now=1000)
    assert not result.allowed
    assert result.reason == reason


@pytest.mark.parametrize("change", [
    {"origin": "pr_workflow"}, {"head_sha": NEXT}, {"status": "failure"},
])
def test_t27_external_workflow_old_sha_or_failed_ci_is_not_evidence(harness, change):
    live, safe, verified = receipts(harness)
    live["ci"][0].update(change)
    result = MergeGate().evaluate(item(), target(), live, safe, verified, {"allowed": True}, now=1000)
    assert not result.allowed and result.reason == "ci_mismatch"


def test_t27_explicit_empty_ci_configuration_can_use_local_tests(harness):
    live, safe, verified = receipts(harness)
    config = target()
    config["ci_sources"] = []
    live["ci"] = []
    assert MergeGate().evaluate(item(), config, live, safe, verified, {"allowed": True}, now=1000).allowed


def test_t27_permission_rechecked_and_missing_permission_cards(harness):
    h = harness
    h.permissions.allowed = False
    result = h.flow.process(item(), dry_run=False, mode="run")
    assert result["status"] == "waiting_user"
    assert h.permissions.cards and not h.github.merges


@pytest.mark.parametrize("mutate", [
    lambda live: live.update(head_sha=NEXT),
    lambda live: live.update(unprocessed_comments=[{"comment_id": "late"}]),
    lambda live: live["ci"][0].update(status="failure"),
    lambda live: live["requirements"][0].update(status="pending"),
])
def test_t27_last_read_catches_changes_after_initial_validation(harness, mutate):
    h = harness
    _, safe, verified = receipts(h)
    h.github.reads = 0
    h.github.on_read = lambda gh, n: mutate(gh.live) if n == 2 else None
    result = h.flow.merge(item(), target(), safe, verified)
    assert result["status"] != "succeeded"
    assert not h.github.merges


@pytest.mark.parametrize("mutate", [
    lambda live: live.update(mergeable=False),
    lambda live: live["ci"][0].update(status="failure"),
    lambda live: live.update(unprocessed_comments=[{"comment_id": "late"}]),
])
def test_t27_final_read_rechecks_gate_after_claim(harness, mutate):
    h = harness
    _, safe, verified = receipts(h)
    h.github.reads = 0
    h.github.on_read = lambda gh, n: mutate(gh.live) if n == 3 else None
    result = h.flow.merge(item(), target(), safe, verified)
    assert result["status"] != "succeeded"
    assert not h.github.merges
    assert h.journal.change_ledger.get("request-1")["state"] == "waiting"


@pytest.mark.parametrize("cause", ["test_failure", "conflict"])
def test_t27_repair_and_conflict_reverify_but_reject_unaccepted_head(harness, cause):
    h = harness
    if cause == "test_failure":
        h.verification.fail_heads.add(HEAD)
    else:
        h.github.live["mergeable"] = False
    result = h.flow.process(item(), dry_run=False, mode="run")
    assert result["reason"] == "pr_identity_mismatch"
    assert h.verification.calls == [HEAD, NEXT]
    assert [op for op, _ in h.work.calls] == ["tdd-change"]
    assert not h.github.merges
    assert h.journal.change_ledger.get("request-1")["state"] == "waiting"
    worker = h.work.calls[0][1]
    assert "body" not in worker and "title" not in worker and "snapshot" not in worker
    assert worker["worker_input"]["summaries"]


@pytest.mark.parametrize("trusted,reason", [(True, "trusted_internal_decision_required"), (False, "untrusted_fix_limit")])
def test_t27_untrusted_never_uses_trusted_exception_route(harness, trusted, reason):
    h = harness
    h.verification.fail_heads.update({HEAD, NEXT})
    h.flow.fix_rounds = 1
    result = h.flow.process(item(trusted=trusted), dry_run=False, mode="run")
    assert result["reason"] == reason
    assert len(h.work.calls) == 1 and not h.github.merges


def test_t27_lost_merge_result_is_reconciled_without_resending(harness):
    h = harness
    def uncertain(gh, repo, pr, sha, method):
        gh.merges.append((repo, pr, sha, method))
        gh.observed[pr] = {"merged": True, "head_sha": sha, "merge_commit_sha": "d" * 40}
        raise OSError("lost reply")
    h.github.on_merge = uncertain
    assert h.flow.process(item(), dry_run=False, mode="run")["reason"] == "merge_unconfirmed"
    assert h.flow.process(item(), dry_run=False, mode="run")["reason"] == "release_pending"
    assert len(h.github.merges) == len(h.release.sent) == 1


def test_t27_unknown_merge_is_parked_not_resent(harness):
    h = harness
    h.github.on_merge = lambda *args: {"merged": False}
    assert h.flow.process(item(), dry_run=False, mode="run")["reason"] == "merge_unconfirmed"
    h.github.on_merge = None
    assert h.flow.process(item(), dry_run=False, mode="run")["reason"] == "merge_unconfirmed"
    assert not h.github.merges


def test_t27_untrusted_evidence_has_two_attempts_for_every_command(harness):
    _, _, receipt = receipts(harness, item(trusted=False))
    assert valid_verification(receipt, repo="owner/code", base=BASE, head=HEAD, untrusted=True)
    receipt["evidence"][-1]["attempt"] = "head-1"
    assert not valid_verification(receipt, repo="owner/code", base=BASE, head=HEAD, untrusted=True)


@pytest.mark.parametrize("change", [
    {"complete": False}, {"timed_out": True}, {"executed": False},
    {"output_truncated": True}, {"head_sha": NEXT}, {"exit_code": 1},
])
def test_t27_incomplete_or_different_test_evidence_is_never_success(harness, change):
    _, _, receipt = receipts(harness)
    receipt["evidence"][0].update(change)
    assert not valid_verification(receipt, repo="owner/code", base=BASE, head=HEAD, untrusted=False)

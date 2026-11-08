"""T-28: individual instruction, delivery confirmation and fake 30-minute wait."""
from copy import deepcopy

from tests.fakes.b14.integration import HEAD, NEXT, item


def test_t28_untrusted_stays_at_card_despite_continuing_permission(harness):
    h = harness
    result = h.flow.process(item(trusted=False), dry_run=False, mode="run")
    assert result["reason"] == "untrusted_instruction_required"
    assert result["status"] == "waiting_user"
    assert len(h.permissions.cards) == 1 and not h.github.merges


def test_t28_wait_starts_on_delivery_and_other_item_advances(harness):
    h = harness
    external = item(trusted=False)
    h.permissions.authorize(external)
    assert h.flow.process(external, dry_run=False, mode="run")["reason"] == "merge_notice_wait"
    h.clock.advance(1799)
    assert h.flow.process(external, dry_run=False, mode="run")["reason"] == "merge_notice_wait"
    assert h.flow.process(item(2), dry_run=False, mode="run")["status"] == "succeeded"
    h.clock.advance(1)
    assert h.flow.process(external, dry_run=False, mode="run")["status"] == "succeeded"
    assert len(h.notices.sent) == 1
    assert len(h.github.merges) == 2


def test_t28_malformed_or_stale_instruction_is_not_authorization(harness):
    h = harness
    it = item(trusted=False)
    h.permissions.authorize(it, NEXT)
    assert h.flow.process(it, dry_run=False, mode="run")["reason"] == "untrusted_instruction_required"
    h.permissions.authorize(it)
    h.permissions.grant["source"] = "commander"
    assert h.flow.process(it, dry_run=False, mode="run")["reason"] == "untrusted_instruction_required"
    assert not h.notices.sent


def test_t28_collect_only_review_bound_to_exact_head(harness):
    h = harness
    it = item(trusted=False)
    h.permissions.authorize(it)
    h.github.live["collect_only_reviewed_head"] = NEXT
    assert h.flow.process(it, dry_run=False, mode="run")["reason"] == "collect_only_required"
    assert not h.github.merges and not h.notices.sent


def test_t28_unknown_notice_is_reconciled_not_resent(harness):
    h = harness
    it = item(trusted=False)
    h.permissions.authorize(it)
    def lost_response(key, destination, body):
        h.notices.observed[key] = {"status": "delivered", "delivered_at": h.clock()}
        raise OSError("response lost")
    h.notices.on_send = lost_response
    assert h.flow.process(it, dry_run=False, mode="run")["reason"] == "merge_notice_required"
    h.clock.advance(120)
    assert h.flow.process(it, dry_run=False, mode="run")["reason"] == "merge_notice_wait"
    assert len(h.notices.sent) == 1
    h.clock.advance(1680)
    assert h.flow.process(it, dry_run=False, mode="run")["status"] == "succeeded"


def test_t28_delivery_rate_limit_defers_timer_until_actual_delivery(harness):
    h = harness
    it = item(trusted=False)
    h.permissions.authorize(it)
    h.notices.result = {"status": "retry", "next_retry": h.clock() + 200}
    assert h.flow.process(it, dry_run=False, mode="run")["reason"] == "merge_notice_required"
    h.clock.advance(199)
    assert h.flow.process(it, dry_run=False, mode="run")["reason"] == "merge_notice_required"
    assert len(h.notices.sent) == 1
    h.clock.advance(1)
    h.notices.result = None
    assert h.flow.process(it, dry_run=False, mode="run")["reason"] == "merge_notice_wait"
    assert not h.github.merges
    h.clock.advance(1800)
    assert h.flow.process(it, dry_run=False, mode="run")["status"] == "succeeded"


def test_t28_head_change_invalidates_notice_and_instruction(harness):
    h = harness
    it = item(trusted=False)
    h.permissions.authorize(it)
    h.flow.process(it, dry_run=False, mode="run")
    h.clock.advance(1800)
    h.github.change_head()
    result = h.flow.process(it, dry_run=False, mode="run")
    assert result["reason"] == "untrusted_instruction_required"
    assert not h.github.merges
    h.permissions.authorize(it, NEXT)
    assert h.flow.process(it, dry_run=False, mode="run")["reason"] == "merge_notice_wait"
    assert len(h.notices.sent) == 2

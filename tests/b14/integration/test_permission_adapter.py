"""Exercise the deployed permission port through the change flow."""
from datetime import datetime, timezone

import pytest

from plugins.subloop.change.integration.permission import PermissionAdapter
from tests.fakes.b14.integration import HEAD, NEXT, item


class Control:
    def check_permission(self, scope):
        return {"allowed": True}


class Cards:
    def __init__(self):
        self.answer = None
        self.requests = []

    def verified_answer(self, scope):
        return self.answer

    def wait(self, item, **scope):
        self.requests.append(scope)
        return "card-1"


def setup(h):
    cards = Cards()
    h.flow.ports.permissions = PermissionAdapter(control=Control(), cards=cards,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    return cards


def approval():
    return {"authenticated": True, "repo": "owner/code", "pr": 1, "head_sha": HEAD,
            "operation": "merge", "decision": "approve", "active": True,
            "expires_at": "2027-01-01T00:00:00Z", "card_id": "card-1", "instruction_ref": "answer-1"}


def test_scoped_answer_allows_untrusted_change_after_notice(harness):
    cards = setup(harness)
    it = item(trusted=False)
    cards.answer = {**approval(), "repo": it["implementation_repo"], "pr": it["pr"]}
    assert harness.flow.process(it, dry_run=False, mode="run")["reason"] == "merge_notice_wait"
    harness.clock.advance(1800)
    assert harness.flow.process(it, dry_run=False, mode="run")["status"] == "succeeded"


@pytest.mark.parametrize("field,value", [
    ("head_sha", NEXT), ("operation", "comment"), ("repo", "other/repo"),
    ("pr", 2), ("authenticated", False), ("active", False),
    ("decision", "deny"), ("expires_at", "2025-01-01T00:00:00Z"),
    ("expires_at", "bad"), ("card_id", ""), ("instruction_ref", ""),
])
def test_unmatched_or_unverified_answer_cannot_authorize(harness, field, value):
    cards = setup(harness)
    it = item(trusted=False)
    cards.answer = {**approval(), "repo": it["implementation_repo"], "pr": it["pr"], field: value}
    it["body"] = "approve"
    it["comments"] = ["approve"]
    result = harness.flow.process(it, dry_run=False, mode="run")
    assert result["reason"] == "untrusted_instruction_required"
    assert cards.requests and not harness.github.merges and not harness.notices.sent


def test_unconfigured_and_unknown_are_denied():
    port = PermissionAdapter()
    assert port.check(project="p", repo="r", destination="github", operation="merge")["allowed"] is False
    assert port.instruction({}, repo="r", pr=1, head_sha=HEAD) is None

from __future__ import annotations

from dataclasses import replace

import pytest

from axcommon import timeutil
from core.control import Reject
from x_posts import Draft, EnvResult, Inspection


def test_t65_targets_are_read_through_existing_control_api(rig):
    from x_posts import ControlAdapter
    from tests.fakes.b16b.x_posts.components import ControlClient

    adapter = ControlAdapter(ControlClient(rig.service))
    assert adapter.targets() == [rig.target.delivery_target()]


def test_t65_start_has_two_real_control_steps(rig):
    assert rig.control.effective("x-posts")["mode"] == "collect_only"
    permission = rig.permission()
    assert rig.engine.send_one(rig.target)["state"] == "skipped"
    assert not rig.env.posts
    rig.start(permission)
    row = rig.drafts("new post")[0]
    assert rig.engine.send_one(rig.target)["state"] == "sent"
    record = rig.engine.journal.get("x-posts", rig.target.destination, row["id"])
    assert record["external_id"] == "12345"
    assert record["state"] == "sent"


def test_t65_run_without_permission_ids_is_rejected(rig):
    with pytest.raises(Reject):
        rig.service.issue({"target": "x-posts", "op": "set_mode", "args": {"mode": "run"},
                           "reason": "試験"}, issued_by="user")
    assert rig.control.effective("x-posts")["mode"] == "collect_only"
    assert rig.env.posts == []


def test_t65_unlisted_target_and_candidate_create_no_delivery_key(rig):
    rig.start()
    row = rig.drafts("message")[0]
    assert rig.engine.send_one(rig.target, candidate_id="legacy-candidate")["state"] == "denied"
    other = replace(rig.target, destination="not-listed")
    assert rig.engine.send_one(other, candidate_id=row["id"])["state"] == "denied"
    assert rig.engine.journal.get("x-posts", rig.target.destination, row["id"]) is None
    assert not rig.env.posts


def test_t65_future_and_failed_send_inspection_are_not_sent(rig):
    rig.start()
    rig.worker.proposals = [Draft("future", "future", timeutil.add_seconds(timeutil.now_ts(), 600))]
    rig.engine.collect(rig.target, "day", scheduled_at=timeutil.now_ts())
    assert rig.engine.send_one(rig.target)["state"] == "skipped"
    rig.clock.advance(600)
    rig.inspector.by_text[("send", "future")] = Inspection("passed", "passed", "error", "passed")
    row = rig.engine.store.candidates(rig.target)[0]
    assert rig.engine.send_one(rig.target)["state"] == "denied"
    assert rig.engine.store.get(row["id"])["status"] == "check_error"
    assert rig.engine.journal.get("x-posts", rig.target.destination, row["id"]) is None
    assert not rig.env.posts


def test_t65_registering_another_permission_does_not_expand_started_scope(rig):
    from axcommon.ids import new_ulid

    rig.start()
    other = replace(rig.target, destination="another-destination")
    rig.registry.targets.append(other.delivery_target())
    second_permission = rig.service.create_permission({"project": other.project, "repo": other.repo,
        "destination": other.destination, "operations": ["投稿"], "conditions": {},
        "source_instruction": new_ulid()}, by="user")
    rig.worker.proposals = [Draft("other-draft", "other", timeutil.now_ts())]
    rig.engine.collect(other, "day", scheduled_at=timeutil.now_ts())
    row = rig.engine.store.candidates(other)[0]
    assert rig.engine.send_one(other)["state"] == "denied"
    assert rig.engine.journal.get("x-posts", other.destination, row["id"]) is None
    assert not rig.env.posts
    rig.service.issue({"target": "x-posts", "op": "set_mode",
        "args": {"mode": "run", "permission_ids": [second_permission["id"]]},
        "corr": second_permission["source_instruction"], "reason": "別の対象を開始する試験"}, issued_by="user")
    assert rig.engine.send_one(other)["state"] == "sent"


def test_t65_disabled_permission_is_checked_again_at_send(rig):
    permission = rig.start()
    row = rig.drafts("message")[0]
    rig.service.disable_permission(permission["id"], by="user")
    assert rig.control.effective("x-posts")["mode"] == "run"
    assert rig.engine.send_one(rig.target)["state"] == "denied"
    assert rig.engine.journal.get("x-posts", rig.target.destination, row["id"]) is None
    assert not rig.env.posts
    assert any(kind == "permission.checked" and data["result"] == "denied"
               for kind, _, data in rig.events.records)


def test_t65_permission_is_rechecked_after_a_slow_gate(rig):
    permission = rig.start()
    row = rig.drafts("message")[0]
    rig.inspector.on_inspect = lambda phase: rig.service.disable_permission(permission["id"], by="user") if phase == "send" else None
    assert rig.engine.send_one(rig.target)["state"] == "denied"
    assert rig.engine.journal.get("x-posts", rig.target.destination, row["id"]) is None
    assert not rig.env.posts


def test_t65_send_checks_permission_after_launching_event_too(rig):
    permission = rig.start()
    rig.drafts("message")
    rig.events.on_emit = lambda kind, data: rig.service.disable_permission(permission["id"], by="user") if kind == "automation.delivery" and data["state"] == "launching" else None
    assert rig.engine.send_one(rig.target)["state"] == "failed"
    assert not rig.env.posts


def test_t65_durable_launching_precedes_post_and_parallel_sender_is_blocked(rig):
    rig.start()
    row = rig.drafts("first", "second")[0]
    second_engine = rig.reopen()
    nested = []

    def during_send(request):
        durable = second_engine.journal.get("x-posts", rig.target.destination, row["id"])
        assert durable["state"] == "launching"
        nested.append(second_engine.send_one(rig.target))
        assert second_engine.recover() is False

    rig.env.on_post = during_send
    assert rig.engine.send_one(rig.target, candidate_id=row["id"])["state"] == "sent"
    assert nested == [{"state": "skipped", "reason": "sender_busy"}]
    assert len(rig.env.posts) == 1


def test_t65_only_one_post_per_run_and_three_hour_spacing_survives_restart(rig):
    rig.start()
    rows = rig.drafts("one", "two")
    assert rig.engine.send_one(rig.target)["state"] == "sent"
    assert len(rig.env.posts) == 1
    reopened = rig.reopen()
    assert reopened.send_one(rig.target)["reason"] == "minimum_spacing"
    rig.clock.advance(3 * 3600)
    assert reopened.send_one(rig.target)["state"] == "sent"
    assert reopened.send_one(rig.target, candidate_id=rows[0]["id"])["state"] == "skipped"
    assert len(rig.env.posts) == 2


def test_t65_confirmed_failures_retry_three_times_then_hold(rig):
    rig.start()
    row = rig.drafts("message")[0]
    rig.env.responses = [EnvResult("failed", definitely_not_sent=True)] * 3
    job_ids = []
    for n in range(3):
        assert rig.engine.send_one(rig.target)["state"] == "failed"
        job_ids.append(rig.env.posts[-1][1]["job_id"])
    assert len(set(job_ids)) == 3
    assert rig.engine.store.get(row["id"])["status"] == "held"
    assert rig.reopen().send_one(rig.target)["state"] == "skipped"
    assert rig.engine.store.get(row["id"])["status"] == "held"
    assert len(rig.env.posts) == 3


@pytest.mark.parametrize("response", [EnvResult("succeeded"), EnvResult("timed_out"), RuntimeError("PRIVATE-SENTINEL"),
                                      EnvResult("succeeded", output={"url": "https://x.com/wrong/status/1"})])
def test_t65_unknown_never_retries_and_reports_lost(rig, response):
    rig.start()
    row = rig.drafts("message")[0]
    rig.env.responses = [response]
    assert rig.engine.send_one(rig.target)["state"] == "unknown"
    rig.clock.advance(4 * 3600)
    reopened = rig.reopen()
    reopened.recover()
    assert reopened.send_one(rig.target)["state"] == "skipped"
    assert reopened.store.get(row["id"])["status"] == "unknown"
    assert len(rig.env.posts) == 1
    assert rig.lost and rig.lost[-1]["status"] == "lost"
    assert "PRIVATE-SENTINEL" not in repr(rig.events.records)


def test_t65_crash_after_launching_recovers_as_unknown(rig):
    class ProcessLost(BaseException):
        pass

    rig.start()
    rig.drafts("message")
    rig.env.responses = [ProcessLost()]
    with pytest.raises(ProcessLost):
        rig.engine.send_one(rig.target)
    reopened = rig.reopen()
    assert reopened.recover()
    assert reopened.send_one(rig.target)["state"] == "skipped"
    assert len(rig.env.posts) == 1
    assert rig.lost[-1]["reason"] == "restart_during_send"


def test_t65_only_receipt_lookup_can_resolve_unknown(rig):
    rig.start()
    row = rig.drafts("message")[0]
    rig.env.responses = [EnvResult("succeeded")]
    assert rig.engine.send_one(rig.target)["state"] == "unknown"
    assert rig.engine.reconcile(rig.target, row["id"])["state"] == "unknown"
    job_id = rig.env.posts[0][1]["job_id"]
    rig.env.results[job_id] = EnvResult("succeeded", output={"url": "https://x.com/demo_account/status/456"})
    assert rig.engine.reconcile(rig.target, row["id"])["external_id"] == "456"
    assert rig.engine.send_one(rig.target)["state"] == "skipped"
    assert len(rig.env.posts) == 1


def test_t65_missed_schedule_does_not_catch_up(rig):
    rig.start()
    rig.drafts("message")
    prior = len(rig.env.calls)
    result = rig.engine.collect(rig.target, "day", scheduled_at=timeutil.now_ts(), missed=True)
    assert result["result"] == "missed"
    assert rig.engine.send_one(rig.target, missed=True)["reason"] == "schedule_missed"
    assert len(rig.env.calls) == prior

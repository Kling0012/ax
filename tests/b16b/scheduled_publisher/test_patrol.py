"""T-63, T-67, T-139: offline scheduled-publisher-specific acceptance boundaries."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from axcommon import ids, schemas, timeutil
from axcommon.redact import Redactor
from core.intake.dest.b16b.common import TargetRegistry
from plugins.timer.scheduled_publisher import Candidate, ScheduledPublisher, Settings
from plugins.timer.scheduled_publisher.checks import decode_line
from plugins.timer.scheduled_publisher.model import PERMISSION_ACTIONS
from tests.fakes.b16b.scheduled_publisher.parts import FakeCLI, FakeControl, FakeEnvironment, FakeWorker


@pytest.fixture(params=["instabox", "box"])
def patrol(request, tmp_path, fake_clock, monkeypatch):
    monkeypatch.setattr("plugins.timer.scheduled_publisher.checks.URL_PREFIXES",
                        ("https://github.com/example-owner/", "https://example-owner.github.io/hibi/"))
    target = {"plugin_id": "scheduled-publisher", "project": "fixture", "repo": "fixture-repo",
              "destination": "fixture-eigen-account", "operations": sorted(PERMISSION_ACTIONS)}
    registry = TargetRegistry([target])
    control = FakeControl()
    cli = FakeCLI()
    env = FakeEnvironment(request.param, cli)
    worker = FakeWorker(env)
    events, lost, cards = [], [], []
    redactor = Redactor(pii_list=["Fixture Private Name"], allow_email_domains=[])
    engine = ScheduledPublisher(tmp_path / "reports" / "scheduled-publisher", registry=registry, control=control,
        environment=env, worker=worker, redactor=redactor, facts="Fixture project facts only.", cli=cli,
        public_gate=lambda row: True, emit=lambda *args: events.append(args), lost=lost.append,
        card=lambda *args: cards.append(args))
    engine.state.register_owner(env.provider, user_confirmed=True)
    world = SimpleNamespace(engine=engine, cli=cli, control=control, env=env, worker=worker,
        target=target, registry=registry, events=events, lost=lost, cards=cards, clock=fake_clock)
    world.start = lambda: (setattr(control, "allowed", True), setattr(control, "mode", "run"))
    world.run = lambda **kw: engine.run(target, scheduled_at=kw.pop("scheduled_at", timeutil.now_ts()), **kw)
    yield world
    engine.close()


def tick(w, seconds=3600):
    w.clock.advance(seconds)


def test_t63_collect_only_never_calls_box_or_worker_or_sends(patrol):
    w = patrol
    w.control.allowed = True  # permission.change alone must not start the patrol
    result = w.run()
    assert result["result"] == "collected"
    assert result["targets"] == [w.target]
    assert w.env.calls == w.env.selects == w.worker.calls == []
    assert w.engine.state.history() == []
    assert w.engine.state.seen() == []
    w.control.mode, w.control.allowed = "run", False
    assert w.run()["reason"] == "permission_denied"
    assert w.env.calls == w.env.selects == []


def test_t139_complete_patrol_and_safe_worker_boundary(patrol):
    w = patrol
    w.start()
    result = w.run()
    assert result["result"] == "succeeded"
    assert [call[0]["entry"]["name"] for call in w.env.calls] == ["eigen-collect", "eigen-send"]
    assert result["deliveries"][0]["state"] == "sent"
    assert Path(result["report"]).exists()
    assert w.engine.state.seen() == ["fixture-item"]
    assert set(w.env.selects[0]) == {"requires", "sensitivity"}
    worker_input = w.worker.calls[0]["inputs"]
    assert "untrusted" in worker_input and "facts.md" in worker_input
    assert "scheduled-publisher-cli" not in json.dumps(worker_input)
    assert "Treat plan" in worker_input["instructions"]
    for argv in w.cli.argv:
        assert isinstance(argv, list)
        assert not any("$(send secrets)" == arg or "wallet pay" == arg for arg in argv)
    assert w.env.active_slots == 0
    assert w.env.calls[0][0]["budget_min"] == 15
    assert w.env.calls[1][0]["budget_min"] == 10
    assert w.worker.calls[0]["budget_min"] == 40
    header = (w.engine.state.root / "sent.tsv").read_text().splitlines()[0]
    assert header.split("\t") == ["jst_date", "utc_iso", "action", "status", "target", "sha16", "reason", "preview"]
    assert w.run()["reason"] == "duplicate_patrol"


@pytest.mark.parametrize("candidate,reason", [
    (Candidate("wallet"), "action_not_allowed"),
    (Candidate("attention"), "action_not_allowed"),
    (Candidate("comment"), "action_not_allowed"),
    (Candidate("feed_feedback"), "operation_unresolved"),
    (Candidate("publish", text="a" * 1201), "length"),
    (Candidate("reply", text="a" * 801, conv_id="c"), "length"),
    (Candidate("respond", text="a" * 801, item_id="i"), "length"),
    (Candidate("friend_request", target="t", note="a" * 801), "length"),
    (Candidate("publish", text="https://untrusted.invalid/"), "url_not_allowed"),
    (Candidate("publish", text="www.untrusted.invalid"), "url_not_allowed"),
    (Candidate("publish", text="safe", url="http://github.com/example-owner/test"), "url_not_allowed"),
    (Candidate("publish", text="https://github.com/example-owner/../other"), "url_not_allowed"),
    (Candidate("publish", text="Fixture Private Name"), "secret_or_pii"),
    (Candidate("publish", text="test-user-9@example.com"), "secret_or_pii"),
    (Candidate("reply", text="Please verify your identity", conv_id="c"), "forbidden_reply_topic"),
    (Candidate("reply", text="We promise payment", conv_id="c"), "forbidden_reply_topic"),
    (Candidate("reply", text="Thanks"), "target_required"),
    (Candidate("publish", text=""), "body_required"),
])
def test_t139_invalid_proposals_are_rejected_with_reason(patrol, candidate, reason):
    w = patrol
    w.start()
    w.worker.candidates = (candidate,)
    result = w.run()
    assert result["rejected"][0]["reason"] == reason
    assert len(w.env.calls) == 1
    assert w.engine.state.history() == []
    assert "Fixture Private Name" not in Path(result["report"]).read_text()


def test_unknown_fields_are_discarded_and_malformed_json_rejected():
    assert decode_line('{"action":"publish","text":"fixture","argv":["remove"]}').text == "fixture"
    with pytest.raises(ValueError):
        decode_line("not json")
    with pytest.raises(ValueError):
        decode_line("[]")
    with pytest.raises(ValueError):
        decode_line("{}")


def test_t139_duplicate_publish_respond_and_reply_within_batch(patrol):
    w = patrol
    w.start()
    a = Candidate("publish", text="fixture")
    b = Candidate("respond", text="fixture", item_id="same-item")
    c = Candidate("reply", text="fixture", conv_id="same-conversation")
    w.worker.candidates = (a, a, b, b, c, c)
    result = w.run()
    assert [row["reason"] for row in result["rejected"]] == ["duplicate_publish", "duplicate_respond", "reply_24h"]
    assert len(result["deliveries"]) == 3
    tick(w)
    result = w.run()
    assert not result["deliveries"]
    assert {row["reason"] for row in result["rejected"]} == {"duplicate_publish", "duplicate_respond", "reply_24h"}
    tick(w, 24 * 3600)
    result = w.run()
    assert len(result["deliveries"]) == 1
    assert result["deliveries"][0]["operation"] == "reply"


def test_t139_day_cap_and_jst_rollover(patrol):
    w = patrol
    w.start()
    w.worker.candidates = tuple(Candidate("reply", text="fixture", conv_id=f"c-{n}") for n in range(7))
    result = w.run()
    assert len(result["deliveries"]) == 5
    assert [row["reason"] for row in result["rejected"]] == ["day_cap", "day_cap"]
    tick(w, 3 * 3600)  # 12:00 UTC -> 15:00 UTC: midnight in JST, still the same UTC date.
    w.worker.candidates = (Candidate("reply", text="fixture", conv_id="new-day"),)
    assert w.run()["deliveries"][0]["state"] == "sent"


def test_t139_run_cap(patrol):
    w = patrol
    w.start()
    w.worker.candidates = tuple(Candidate("publish", text=f"fixture-{n}") for n in range(15))
    result = w.run()
    assert len(result["deliveries"]) == 12
    assert [row["reason"] for row in result["rejected"]] == ["run_cap"] * 3


def test_t139_gate_applies_to_notes_and_all_bodies_and_fails_closed(patrol):
    w = patrol
    w.start()
    w.engine.checker.public_gate = lambda row: False
    w.worker.candidates = (Candidate("publish", text="fixture"), Candidate("reply", text="hello", conv_id="c"),
        Candidate("respond", text="hello", item_id="i"), Candidate("friend_request", target="t", note="hello"),
        Candidate("friend_accept", target="t"))
    result = w.run()
    assert {row["reason"] for row in result["rejected"]} == {"public_gate"}
    assert len(w.env.calls) == 1


@pytest.mark.parametrize("failure", ["no_receipt", "no_launched", "fail_send"])
def test_t139_unknown_not_retried_even_after_box_switch(patrol, failure):
    w = patrol
    w.start()
    setattr(w.cli, failure, True)
    result = w.run()
    assert result["deliveries"][0]["state"] == "unknown"
    assert w.lost and w.lost[-1]["status"] == "lost"
    w.env.provider = "box" if w.env.provider == "instabox" else "instabox"
    tick(w)
    assert w.run()["reason"] == "identity_owner_registration_required"
    with pytest.raises(PermissionError):
        w.engine.state.register_owner(w.env.provider)
    w.engine.state.register_owner(w.env.provider, user_confirmed=True)
    tick(w)
    result = w.run()
    assert not result["deliveries"]
    assert len([call for call in w.env.calls if call[0]["entry"]["name"] == "eigen-send"]) == 1
    assert w.engine.state.history()[0]["state"] == "unknown"


def test_t139_sent_survives_reopen_and_switch(patrol):
    w = patrol
    w.start()
    assert w.run()["deliveries"][0]["state"] == "sent"
    from plugins.timer.scheduled_publisher.state import State
    root = w.engine.state.root
    w.engine.state.close()
    w.engine.state = State(root, w.registry, emit=w.engine._emit, lost=w.lost.append)
    w.env.provider = "box" if w.env.provider == "instabox" else "instabox"
    w.engine.state.register_owner(w.env.provider, user_confirmed=True)
    tick(w)
    result = w.run()
    assert result["deliveries"] == []
    assert result["rejected"][0]["reason"] == "duplicate_publish"
    assert len(w.cli.launched) == 1


def test_t139_launch_is_durable_before_env_call_and_crash_recovers_unknown(patrol):
    w = patrol
    w.start()
    def check_before_send():
        assert w.engine.state.history()[0]["state"] == "launching"
    w.env.before_send = check_before_send
    assert w.run()["deliveries"][0]["state"] == "sent"
    with w.engine.state.journal.db:
        w.engine.state.journal.db.execute("UPDATE deliveries SET state='launching', external_id=NULL")
    tick(w)
    w.run()
    assert w.engine.state.history()[0]["state"] == "unknown"
    assert len(w.cli.launched) == 1


def test_t139_confirmed_not_sent_can_retry(patrol):
    w = patrol
    w.start()
    w.cli.not_sent = True
    assert w.run()["deliveries"][0]["state"] == "failed"
    w.cli.not_sent = False
    tick(w)
    assert w.run()["deliveries"][0]["state"] == "sent"


def test_t139_unavailable_boxes_and_collection_failures_stay_local(patrol):
    w = patrol
    w.start()
    w.env.available = False
    assert w.run()["result"] == "parked"
    w.env.available = True
    w.cli.fail_collect = True
    tick(w)
    assert w.run()["reason"] == "collection_failed"
    tick(w)
    w.run()
    assert w.worker.calls == []
    assert any(event[0] == "resource.repeat_failure" for event in w.events)
    # Another caller continues: no global stop, mode edit, or service action.
    assert w.control.mode == "run"
    w.cli.fail_collect = False
    tick(w)
    assert w.run()["result"] == "succeeded"


def test_t63_unverified_capability_never_runs(patrol):
    w = patrol
    w.start()
    w.env.capability_ok = False
    assert w.run()["reason"] == "capability_unverified"
    assert w.env.calls == []


def test_t67_revocation_during_decision_blocks_sending(patrol):
    w = patrol
    w.start()
    w.worker.on_decide = lambda: setattr(w.control, "allowed", False)
    result = w.run()
    assert not result["deliveries"]
    assert w.engine.state.history() == []
    assert len(w.env.calls) == 1


def test_t67_overlap_is_skipped(patrol):
    w = patrol
    w.start()
    with w.engine.state.single_run() as held:
        assert held
        assert w.run()["reason"] == "overlap"
    assert not w.env.calls


def test_unconfirmed_cli_contract_parks_without_box_exec(patrol):
    from plugins.timer.scheduled_publisher.model import UnconfirmedCLI
    w = patrol
    w.start()
    w.engine.cli = UnconfirmedCLI()
    assert w.run()["reason"] == "unconfirmed_contract"
    assert not w.env.calls


def test_t67_reports_and_events_are_redacted(patrol):
    w = patrol
    w.start()
    w.worker.report = "Fixture Private Name should not appear in saved output."
    result = w.run()
    assert "Fixture Private Name" not in Path(result["report"]).read_text()
    assert "Fixture Private Name" not in json.dumps(w.events)


def test_recent_publishes_and_search_dedup_are_worker_inputs(patrol):
    w = patrol
    w.start()
    w.run()
    tick(w)
    w.run()
    inputs = w.worker.calls[-1]["inputs"]
    assert inputs["recent_publish_previews"] == ["Fixture project update"]
    assert inputs["seen_item_ids"] == ["fixture-item"]
    tick(w, 73 * 3600)
    w.run()
    assert w.worker.calls[-1]["inputs"]["recent_publish_previews"] == []


def test_permission_target_mismatch_is_rejected_before_box(patrol):
    target = {**patrol.target, "destination": "not-listed"}
    assert patrol.engine.run(target, scheduled_at=timeutil.now_ts())["reason"] == "target_not_listed"
    assert not patrol.env.calls


def test_t67_revocation_during_last_gate_prevents_launch(patrol):
    w = patrol
    w.start()
    calls = []
    def gate(row):
        calls.append(row)
        if len(calls) == 2:
            w.control.allowed = False
        return True
    w.engine.checker.public_gate = gate
    result = w.run()
    assert len(w.env.calls) == 1
    assert result["deliveries"][0]["state"] == "denied"
    assert w.engine.state.history() == []


def test_t139_row_error_does_not_stop_valid_proposals(patrol):
    w = patrol
    w.start()
    w.worker.candidates = (Candidate("publish", text="https://[bad-host/"),
                           Candidate("publish", text="A valid fixture proposal"))
    result = w.run()
    assert result["rejected"][0]["reason"] == "url_not_allowed"
    assert len(result["deliveries"]) == 1
    assert result["deliveries"][0]["state"] == "sent"


def test_exact_length_and_registered_urls_are_allowed(patrol):
    w = patrol
    w.start()
    w.worker.candidates = (
        Candidate("publish", text="a" * 1200, url="https://github.com/example-owner/fixture"),
        Candidate("reply", text="a" * 800, conv_id="boundary-conversation"),
        Candidate("respond", text="https://example-owner.github.io/hibi/", item_id="boundary-item"),
        Candidate("friend_request", target="boundary-user", note="a" * 800),
    )
    assert len(w.run()["deliveries"]) == 4


def test_default_gate_fails_closed(patrol):
    from plugins.timer.scheduled_publisher.engine import missing_gate
    w = patrol
    w.start()
    w.engine.checker.public_gate = missing_gate
    assert w.run()["rejected"][0]["reason"] == "public_gate_unavailable"
    assert len(w.env.calls) == 1


def test_offline_artifacts_are_saved_on_ax_side(patrol):
    w = patrol
    w.start()
    result = w.run()
    directory = w.engine.state.root / result["run_id"]
    for name in ("plan.json", "messages.json", "relations.json", "skipped.json",
                 "outbox.jsonl", "approved.jsonl", "rejected.jsonl", "sent.jsonl"):
        assert (directory / name).is_file()
    skipped = json.loads((directory / "skipped.json").read_text())
    assert len(skipped) == 2
    assert skipped[0]["reason"] == "not_allowlisted"
    assert all("fixture" in row.subject for row in w.cli.launched if row.subject)


def test_manifest_and_graph_document_schema(repo_root):
    directory = repo_root / "plugins/timer/scheduled_publisher"
    schemas.validate("manifest", json.loads((directory / "manifest.yaml").read_text()))
    graph = json.loads((directory / "graph.yaml").read_text())
    schemas.validate("graph", graph)
    assert [node["type"] for node in graph["nodes"]] == [
        "eigen.preflight", "env.run", "worker.run", "eigen.outbox_check", "env.run", "eigen.record"]
    assert graph["nodes"][0]["needs_mode"] == ["run", "collect_only"]
    assert all(node["needs_mode"] == ["run"] for node in graph["nodes"][1:-1])


def test_production_runtime_binding_is_explicitly_unimplemented():
    from plugins.timer.scheduled_publisher.integration import bind_runtime
    with pytest.raises(NotImplementedError):
        bind_runtime()


def test_legacy_delivery_is_read_as_current_and_never_resent(tmp_path, monkeypatch):
    import sqlite3
    from core.intake.dest.b16b.delivery import DeliveryJournal

    legacy = 'fixture-legacy-publisher'
    monkeypatch.setenv('AX_PUBLISH_LEGACY_PLUGIN_IDS', legacy)
    target = {'plugin_id': legacy, 'project': 'fixture', 'repo': 'fixture',
              'destination': 'fixture', 'operations': ['publish']}
    path = tmp_path / 'targets.json'
    path.write_text(json.dumps({'targets': [target]}))
    registry = TargetRegistry.load(path)
    current = registry.for_plugin('scheduled-publisher')[0]
    dbpath = tmp_path / 'delivery.sqlite3'
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE deliveries (plugin_id TEXT NOT NULL, destination TEXT NOT NULL, '
                   'source_item_id TEXT NOT NULL, operation TEXT NOT NULL, state TEXT NOT NULL, '
                   'external_id TEXT, checked_at TEXT NOT NULL, reason TEXT NOT NULL, '
                   'PRIMARY KEY(plugin_id, destination, source_item_id))')
        db.execute('INSERT INTO deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   (legacy, 'fixture', 'item', 'publish', 'sent', 'receipt', timeutil.now_ts(), ''))
    journal = DeliveryJournal(dbpath, registry, emit=lambda *a: None, lost=lambda *a: None)
    try:
        calls = []
        result = journal.deliver(mode='run', target=current, operation='publish', source_item_id='item',
            registered=True, expected_destination='fixture', permission_reader=SimpleNamespace(check=lambda *a: True),
            preflight=lambda: True, send=lambda: calls.append('sent'))
        assert result['plugin_id'] == 'scheduled-publisher'
        assert result['state'] == 'sent' and calls == []
        assert [row[0] for row in journal.db.execute('SELECT plugin_id FROM deliveries')] == ['scheduled-publisher']
    finally:
        journal.close()


def test_control_protects_current_and_configured_legacy_outputs(tmp_path):
    import os
    import subprocess
    import sys

    script = '''
from core.control.service import ControlService
from core.control.store import Store
from axcommon.timeutil import now_ts
import sys
store = Store(sys.argv[1])
try:
    for plugin_id, output in (("current", "scheduled-publisher.publish"),
                              ("legacy", "legacy-publisher.reply"),
                              ("other", "unrelated.publish")):
        store.upsert_plugin({"id": plugin_id, "kind": "timer", "path": "fixture",
            "manifest": {"id": plugin_id, "capabilities": ["run", "collect_only"],
                         "outputs": [output], "knobs": {}}, "registered_at": now_ts()})
    service = ControlService(store, None)
    print(*(service.effective(name)["mode"] for name in ("current", "legacy", "other")))
finally:
    store.close()
'''
    for index, (legacy_ids, expected) in enumerate((('', 'collect_only run run'),
                                                    (' legacy-publisher , ', 'collect_only collect_only run'))):
        env = {**os.environ, 'AX_PUBLISH_LEGACY_PLUGIN_IDS': legacy_ids}
        result = subprocess.run([sys.executable, '-c', script, str(tmp_path / f'control-{index}.sqlite')],
                                env=env, text=True, capture_output=True, check=True)
        assert result.stdout.strip() == expected

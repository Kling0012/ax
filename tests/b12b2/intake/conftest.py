from __future__ import annotations

import pytest

from core.intake.approval import ApprovalService, ApprovalStore
from core.intake.approval.audit import ApprovalAudit
from core.intake.approval.go import GoConfirm
from core.intake.approval.policy import AuthorityPolicy
from core.intake.audit import AccessLog
from tests.fakes.b12b2.intake.parts import FakeExecutor, FakeHQ, FakeSeatState, schema_for


@pytest.fixture
def world(tmp_path, fake_clock, fake_transport):
    from axcommon.client import TimelineClient
    from axcommon.events import EventSink
    fake_transport.add("POST", "/v1/events", json={"ok": True})
    sink = EventSink(TimelineClient("http://fake:8080", transport=fake_transport), "intake", "fake-host", tmp_path / "outbox")
    audit = ApprovalAudit(sink=sink, access=AccessLog(tmp_path / "access.log"), caller_reader=lambda ident: {
        "device": "device-a", "session": "verified-session", "key_fp": "test-fingerprint",
        "declared": {"agent": "fake-agent", "model": "fake-model"}})
    state, executor, cards, notices = FakeSeatState(), FakeExecutor(), [], []
    policy = AuthorityPolicy(schema_loader=schema_for, seat_check=state,
                             manifest_reader=lambda target: {"outputs": ["github.comment"]})
    store = ApprovalStore(tmp_path / "intake.sqlite")
    service = ApprovalService(store, policy=policy, audit=audit, executor=executor,
                              go=GoConfirm(store, schema_loader=schema_for),
                              card_writer=cards.append, notifier=notices.append)
    yield {"service": service, "store": store, "state": state, "executor": executor, "cards": cards,
           "notices": notices, "hq": FakeHQ(), "clock": fake_clock, "transport": fake_transport,
           "audit": audit, "sink": sink, "tmp": tmp_path}
    store.close()

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from axcommon import ids
from core.control import Reject
from core.intake.dest.b16b.delivery import DeliveryJournal
from tests.b16b.conftest import start


def permit(world):
    return world.control.create_permission(world.permission, by="user")


def test_outbound_capability_is_checked_before_permission(world):
    plugin = world.store.get_plugin(world.plugin_id)
    plugin["manifest"]["capabilities"] = ["collect_only"]
    world.store.upsert_plugin(plugin)
    with pytest.raises(Reject) as exc_info:
        start(world)
    assert exc_info.value.status == 400
    assert exc_info.value.code == "capability_mismatch"
    assert world.control.plugin_view(world.plugin_id)["mode"] == "collect_only"
    assert not world.sender.calls


def test_t152_two_steps(world):
    assert world.control.plugin_view(world.plugin_id)["mode"] == "collect_only"
    assert world.send()["state"] == "skipped"
    permission = permit(world)
    assert world.send()["state"] == "skipped"
    assert not world.sender.calls
    assert world.journal.get(world.plugin_id, world.target["destination"], "ax-fictional-candidate") is None
    command = start(world, [permission["id"]])
    assert command["expires_at"] is None
    assert world.control.plugin_view(world.plugin_id)["mode"] == "run"
    assert world.send()["state"] == "sent"
    assert world.send()["state"] == "sent"
    assert len(world.sender.calls) == 1
    assert any(e["kind"] == "automation.delivery" and e["data"]["state"] == "sent" for e in world.events.events)


@pytest.mark.parametrize("bad", ["missing_ids", "unknown_id", "disabled", "wrong_scope", "wrong_source"])
def test_t152_rejected_start_preserves_mode(world, bad):
    permission = permit(world)
    permission_ids = [permission["id"]]
    overrides = {}
    if bad == "missing_ids":
        permission_ids = []
    elif bad == "unknown_id":
        permission_ids = [ids.new_ulid()]
    elif bad == "disabled":
        world.control.disable_permission(permission["id"], by="user")
    elif bad == "wrong_scope":
        permission["operations"].append("unknown-operation")
        world.store.put_permission(permission)
    else:
        overrides["corr"] = ids.new_ulid()
    with pytest.raises(Reject):
        start(world, permission_ids, **overrides)
    assert world.control.effective(world.plugin_id)["mode"] == "collect_only"
    assert not world.sender.calls
    assert any(e["kind"] == "control.rejected" for e in world.events.events)


@pytest.mark.parametrize("actor", ["manager", "backup", "inspection"])
def test_t152_internal_roles_cannot_authorize(world, actor):
    with pytest.raises(Reject):
        world.control.create_permission(world.permission, by=actor)
    permission = permit(world)
    with pytest.raises(Reject):
        world.control.disable_permission(permission["id"], by=actor)
    with pytest.raises(Reject):
        world.control.issue({"target": world.plugin_id, "op": "set_mode",
            "args": {"mode": "run", "permission_ids": [permission["id"]]},
            "reason": "fictional", "corr": world.reference}, issued_by=actor)
    assert not world.sender.calls


@pytest.mark.parametrize("bad", ["empty", "unknown_destination", "partly_unknown", "wildcard"])
def test_t153_bad_permissions_are_atomic(world, bad):
    permission = dict(world.permission)
    if bad == "empty":
        permission["operations"] = []
    elif bad == "unknown_destination":
        permission["destination"] = "other-destination"
    elif bad == "partly_unknown":
        permission["operations"] = [world.operation, "unknown-operation"]
    else:
        permission["repo"] = "*"
    with pytest.raises(Reject):
        world.control.create_permission(permission, by="user")
    assert not world.store.list_permissions()


def test_t153_disable_is_checked_each_time(world):
    permission = permit(world)
    start(world, [permission["id"]])
    assert world.send()["state"] == "sent"
    world.control.disable_permission(permission["id"], by="user")
    assert world.control.effective(world.plugin_id)["mode"] == "run"
    assert world.send(source_item_id="ax-new-candidate")["state"] == "denied"
    assert len(world.sender.calls) == 1
    assert world.journal.get(world.plugin_id, world.target["destination"], "ax-new-candidate") is None
    assert any(e["kind"] == "permission.checked" and e["data"]["result"] == "denied" for e in world.events.events)


@pytest.mark.parametrize("bad", ["unregistered_candidate", "channel_mismatch", "operation_outside", "gate_failed"])
def test_t153_no_key_for_outside_candidates(world, bad):
    permission = permit(world)
    start(world, [permission["id"]])
    overrides = {"registered": False} if bad == "unregistered_candidate" else (
        {"expected_destination": "other-channel"} if bad == "channel_mismatch" else (
            {"operation": "other-operation"} if bad == "operation_outside" else {"preflight": lambda: False}))
    assert world.send(**overrides)["state"] == "denied"
    assert not world.sender.calls
    assert world.journal.get(world.plugin_id, world.target["destination"], "ax-fictional-candidate") is None


@pytest.mark.parametrize("outcome", ["unknown", "empty"])
def test_t67_unknown_is_lost_and_never_retried(world, outcome):
    permission = permit(world)
    start(world, [permission["id"]])
    world.sender.outcome = outcome
    assert world.send()["state"] == "unknown"
    world.sender.outcome = "sent"
    assert world.send()["state"] == "unknown"
    assert len(world.sender.calls) == 1
    assert world.events.cards[-1]["status"] == "lost"


def test_t67_launching_survives_crash(world, tmp_path):
    permission = permit(world)
    start(world, [permission["id"]])
    world.sender.outcome = "crash"
    with pytest.raises(SystemExit):
        world.send()
    assert world.journal.get(world.plugin_id, world.target["destination"], "ax-fictional-candidate")["state"] == "launching"
    restarted = DeliveryJournal(tmp_path / "delivery.sqlite3", world.registry, emit=world.events.emit, lost=world.events.lost)
    try:
        assert restarted.recover() == 1
        assert restarted.get(world.plugin_id, world.target["destination"], "ax-fictional-candidate")["state"] == "unknown"
        assert world.send()["state"] == "unknown"
        assert len(world.sender.calls) == 1
    finally:
        restarted.close()


def test_t67_failed_send_can_retry(world):
    permission = permit(world)
    start(world, [permission["id"]])
    world.sender.outcome = "failed"
    assert world.send()["state"] == "failed"
    world.sender.outcome = "sent"
    assert world.send()["state"] == "sent"
    assert len(world.sender.calls) == 2


def test_t67_parallel_claim_is_unique(world):
    permission = permit(world)
    start(world, [permission["id"]])
    entered, release = Event(), Event()

    def send():
        entered.set()
        assert release.wait(2)
        return world.sender()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(world.send, send=send)
        assert entered.wait(2)
        second = world.send()
        release.set()
        assert first.result()["state"] == "sent"
    assert second["state"] == "launching"
    assert len(world.sender.calls) == 1


def test_registry_failure_does_not_stop_other_plugins(world):
    from core.intake.dest.b16b.common import install_control
    from core.control.service import ControlService
    from axcommon import timeutil

    world.store.upsert_plugin({"id": "fictional-observer", "kind": "timer", "path": "fictional/manifest.yaml",
        "manifest": {"capabilities": ["run", "collect_only", "draining"], "outputs": [], "knobs": {}},
        "registered_at": timeutil.now_ts()})

    def unavailable():
        raise ValueError("fictional malformed inventory")

    control = install_control(ControlService(world.store, world.events, applier_transport=world.transport), unavailable)
    permission = control.create_permission({"project": "fictional", "repo": "fictional-repo",
        "destination": "local", "operations": ["plugin.run:fictional-observer"],
        "source_instruction": world.reference}, by="user")
    command = control.issue({"target": "fictional-observer", "op": "set_mode",
                             "args": {"mode": "run", "permission_ids": [permission["id"]]},
                             "reason": "fictional observation"}, issued_by="user")
    assert command["state"] == "active"
    with pytest.raises(Reject):
        control.issue({"target": world.plugin_id, "op": "set_mode", "args": {"mode": "run"},
                       "reason": "fictional external start"}, issued_by="user")


def test_missing_outbound_target_is_rejected_before_delivery_validation(world):
    from core.control import Reject
    from core.intake.dest.b16b.common import DeliveryScopeValidator, PLUGIN_IDS

    assert isinstance(world.control.validate_delivery_scope, DeliveryScopeValidator)
    missing_target = next(plugin_id for plugin_id in PLUGIN_IDS if plugin_id != world.plugin_id)
    with pytest.raises(Reject) as exc_info:
        world.control.issue({"target": missing_target, "op": "set_mode", "args": {"mode": "run"},
                             "reason": "missing outbound target"}, issued_by="user")

    assert exc_info.value.code == "unknown_target"
    assert not world.store.living_commands()


def test_unavailable_target_registry_rejects_outbound_run(world):
    from core.control import Reject
    from core.control.service import ControlService
    from core.intake.dest.b16b.common import install_control

    def unavailable():
        raise OSError("target inventory unavailable")

    control = install_control(ControlService(world.store, world.events, applier_transport=world.transport), unavailable)
    with pytest.raises(Reject) as exc_info:
        control.issue({"target": world.plugin_id, "op": "set_mode", "args": {"mode": "run"},
                       "reason": "target inventory unavailable"}, issued_by="user")

    assert exc_info.value.code == "target_list_unavailable"
    assert not world.store.living_commands()


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        pytest.param(OSError, "target registry unavailable", id="os-error"),
        pytest.param(ValueError, "target registry returned invalid data", id="invalid-data"),
        pytest.param(TimeoutError, "target registry timed out", id="timeout"),
    ],
)
def test_external_sending_plugin_rejects_unavailable_target_registry(world, error_type, message):
    from axcommon import timeutil
    from core.control import Reject
    from core.control.service import ControlService
    from core.intake.dest.b16b.common import install_control

    plugin_id = "fictional-external-sender"
    world.store.upsert_plugin({"id": plugin_id, "kind": "timer", "path": "fictional/manifest.yaml",
        "manifest": {"id": plugin_id, "mode": "collect_only", "capabilities": ["run", "collect_only", "draining"],
                     "outputs": ["post"], "knobs": {}, "execution": {"endpoint": "http://fictional:8080"}},
        "registered_at": timeutil.now_ts()})

    def unavailable():
        raise error_type(message)

    control = install_control(ControlService(world.store, world.events, applier_transport=world.transport), unavailable)
    with pytest.raises(Reject) as exc_info:
        control.issue({"target": plugin_id, "op": "set_mode", "args": {"mode": "run"},
                       "reason": "target registry unavailable"}, issued_by="user")

    assert exc_info.value.code == "target_list_unavailable"
    assert not world.store.living_commands()

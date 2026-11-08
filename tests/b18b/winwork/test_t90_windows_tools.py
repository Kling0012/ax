"""T-90: explicit-only Windows calls, owner use, one desktop and local failure."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Event

import pytest
import yaml
from jsonschema import Draft202012Validator

from axcommon import schemas
from core.resources.catalog import validate_resource
from core.resources.winwork import (
    DesktopState, ExecutionResult, UnconfiguredExecutor, WindowsTools,
    load_tools, register_tools_config,
)
from tests.fakes.b18b.winwork import FakeEvents, FakeExecutor


@pytest.fixture
def setup(repo_root, tmp_path):
    tools = load_tools(repo_root / "config/windows-tools.example.yaml")
    profile = {tool["location"]: f"fake-{tool['id']}" for tool in tools}
    runner, events = FakeExecutor(), FakeEvents()
    def service(executor=runner):
        return WindowsTools(tools, profile=profile, state_dir=tmp_path / "slots",
                            events=events, executor=executor)
    return tools, profile, runner, events, service


def request(tool_id="remote-device-b", sensitivity="public", **kw):
    return {"tool_id": tool_id, "target": {"kind": "app", "value": "Notepad"},
            "instruction": "Type the supplied text", "sensitivity": sensitivity, **kw}


def test_t90_explicit_tool_call_and_safe_timeline(setup):
    tools, profile, runner, events, service = setup
    job = request()
    job["instruction"] = "UNLOGGED_INSTRUCTION"
    job["target"] = {"kind": "url", "value": "https://example.invalid/UNLOGGED_TARGET"}
    result = service().call(job)
    assert result["status"] == "succeeded"
    assert runner.calls[0][1] == profile[tools[1]["location"]]
    assert runner.calls[0][2] == job
    assert [e["data"]["status"] for e in events.events] == ["running", "succeeded"]
    serialized = json.dumps(events.events)
    assert "UNLOGGED" not in serialized
    assert "fake-remote-device-b" not in serialized
    assert "fake-output" not in serialized


@pytest.mark.parametrize("change", [
    {"tool_id": ""}, {"tool_id": "unknown"}, {"tool_id": None},
    {"target": {}}, {"target": {"kind": "app", "value": "  "}},
    {"target": {"kind": "url", "value": "no-explicit-url"}},
    {"instruction": "  "}, {"requires": ["windows-computer-use"]},
    {"model": {"data_classes": ["private"]}},
])
def test_t90_missing_or_implicit_destination_is_rejected(setup, change):
    _, _, runner, _, service = setup
    job = request(user_requested=True)
    job.update(change)
    assert service().call(job)["status"] == "rejected"
    assert runner.calls == []


def test_t90_no_tool_id_even_user_requested(setup):
    *_, service = setup
    job = request(user_requested=True)
    del job["tool_id"]
    assert service().call(job)["status"] == "rejected"


@pytest.mark.parametrize("owner", [True, None])
@pytest.mark.parametrize("user_requested", [True, False])
def test_t90_owner_use_never_overridden(setup, owner, user_requested):
    _, _, runner, _, service = setup
    runner.states["remote-device-b"] = DesktopState(True, True, owner)
    assert service().call(request(user_requested=user_requested))["reason"] in ("owner_use", "owner_use_unknown")
    assert not runner.calls


@pytest.mark.parametrize("state,reason", [
    (DesktopState(False, True, False), "desktop_unavailable"),
    (DesktopState(True, None, False), "desktop_busy"),
    (DesktopState(True, False, False), "desktop_busy"),
])
def test_t90_unavailable_desktop_stays_local(setup, state, reason):
    _, _, runner, _, service = setup
    runner.states["remote-device-b"] = state
    svc = service()
    assert svc.call(request())["reason"] == reason
    assert svc.call(request("remote-device-a"))["status"] == "succeeded"


def test_t90_shared_slot_across_service_instances_and_other_desktop_progress(setup):
    _, _, runner, _, service = setup
    entered, release = Event(), Event()
    def hold(tool, job):
        if tool["id"] == "remote-device-b":
            entered.set()
            assert release.wait(5)
    runner.on_run = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service().call, request())
        try:
            assert entered.wait(5)
            assert service().call(request())["reason"] == "slot_busy"
            assert service().call(request("remote-device-a"))["status"] == "succeeded"
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "succeeded"
    assert service().call(request())["status"] == "succeeded"


@pytest.mark.parametrize("sensitivity,family,expected", [
    ("public", None, "succeeded"),
    ("private", None, "rejected"),
    ("sensitive", None, "rejected"),
    ("private", {"data_classes": ["public"], "tier": "standard"}, "rejected"),
    ("private", {"data_classes": ["public", "private"], "tier": "free"}, "succeeded"),
    ("sensitive", {"data_classes": ["public", "private", "sensitive"], "tier": "free"}, "rejected"),
    ("sensitive", {"data_classes": ["sensitive"], "tier": "standard"}, "succeeded"),
    ("private", {"data_classes": [], "tier": "standard"}, "rejected"),
])
def test_t90_effective_class_is_tool_and_actual_llm_intersection(setup, tmp_path, sensitivity, family, expected):
    tools, profile, runner, events, _ = setup
    tools[1]["data_classes"] = ["public", "private", "sensitive"]
    runner.states["remote-device-b"] = DesktopState(True, True, False, family)
    svc = WindowsTools(tools, profile=profile, state_dir=tmp_path, events=events, executor=runner)
    result = svc.call(request(sensitivity=sensitivity))
    assert result["status"] == expected
    if runner.calls:
        assert runner.calls[-1][2]["sensitivity"] == sensitivity


def test_t90_tool_classes_cannot_be_widened_by_family(setup):
    _, _, runner, _, service = setup
    runner.states["remote-device-b"] = DesktopState(True, True, False, {"data_classes": ["private"], "tier": "standard"})
    assert service().call(request(sensitivity="private"))["reason"] == "data_class_denied"
    assert not runner.calls


def test_t90_ambiguous_execution_not_retried_even_after_controller_restart(setup):
    _, _, runner, events, service = setup
    runner.run_error = RuntimeError("DO_NOT_LOG_CREDENTIAL_OR_ADDRESS")
    assert service().call(request())["status"] == "lost"
    runner.run_error = None
    svc = service()
    assert svc.call(request())["reason"] == "reconciliation_required"
    runner.states["remote-device-b"] = DesktopState(True, False, False)
    assert svc.reconcile("remote-device-b") is False
    assert svc.call(request("remote-device-a"))["status"] == "succeeded"
    runner.states["remote-device-b"] = DesktopState(True, True, False)
    assert svc.reconcile("remote-device-b") is True
    assert svc.call(request())["status"] == "succeeded"
    assert "DO_NOT_LOG" not in json.dumps(events.events)


@pytest.mark.parametrize("bad_result", [None, ExecutionResult("lost"), ExecutionResult("running")])
def test_t90_nonterminal_result_keeps_slot_uncertain(setup, bad_result):
    _, _, runner, _, service = setup
    runner.result = bad_result
    assert service().call(request())["status"] == "lost"
    assert service().call(request())["reason"] == "reconciliation_required"


def test_t90_known_failure_releases_only_its_slot(setup):
    _, _, runner, _, service = setup
    runner.result = ExecutionResult("failed")
    assert service().call(request())["status"] == "failed"
    runner.result = ExecutionResult("succeeded")
    assert service().call(request())["status"] == "succeeded"


def test_t90_missing_profile_and_transport_are_local(setup, tmp_path):
    tools, _, runner, events, service = setup
    missing = WindowsTools(tools, profile={}, state_dir=tmp_path, events=events, executor=runner)
    assert missing.call(request())["reason"] == "profile_missing"
    assert not runner.probes
    assert service(UnconfiguredExecutor()).call(request())["reason"] == "not_implemented"
    assert service().call(request())["status"] == "succeeded"


def test_t90_probe_exception_does_not_remove_other_tools(setup):
    _, _, runner, _, service = setup
    runner.probe_error = RuntimeError("DO_NOT_LOG")
    assert service().call(request())["reason"] == "probe_failed"
    runner.probe_error = None
    assert service().call(request("remote-device-a"))["status"] == "succeeded"


def test_t90_tools_rejected_by_existing_environment_and_model_registries(setup):
    tools, *_ = setup
    accepted, rejected = schemas.load_environments(tools)
    assert accepted == []
    assert len(rejected) == 2
    assert all(validate_resource(tool) for tool in tools)
    # Even capabilities that satisfy a request cannot make a Windows adapter valid.
    for tool in tools:
        forged = {**tool, "adapter": "windows-computer-use", "capabilities": ["worker-host", "gpu"],
                  "worker": {}, "state": "available"}
        assert schemas.errors("environment", forged)
        assert validate_resource(forged)


@pytest.mark.parametrize("change", [
    {"max_concurrent": 2}, {"max_concurrent": True},
    {"location": "actual-host.invalid"}, {"credential": "not-allowed"},
    {"kind": "environment"}, {"data_classes": ["unknown"]},
])
def test_catalog_rejects_unsafe_configuration(setup, tmp_path, change):
    tools, *_ = setup
    tools[0].update(change)
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"tools": tools}), encoding="utf-8")
    with pytest.raises(schemas.ValidationFailed):
        load_tools(path)


def test_catalog_rejects_aliases(setup, tmp_path):
    tools, *_ = setup
    tools.append({**deepcopy(tools[0]), "id": "alias"})
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"tools": tools}), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate desktop"):
        load_tools(path)


def test_package_schemas_are_valid(repo_root):
    for path in (repo_root / "schemas/ext/b18b/winwork").glob("*.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_shared_tools_config_integration_is_explicitly_unimplemented():
    with pytest.raises(NotImplementedError, match="include contract"):
        register_tools_config()


def test_profile_aliases_cannot_bypass_desktop_slot(setup, tmp_path):
    tools, profile, runner, events, _ = setup
    profile[tools[0]["location"]] = profile[tools[1]["location"]]
    with pytest.raises(ValueError, match="duplicate resolved desktop"):
        WindowsTools(tools, profile=profile, state_dir=tmp_path, events=events, executor=runner)


def test_timeline_failure_never_starts_desktop_and_remains_local(setup, tmp_path):
    tools, profile, runner, _, _ = setup
    class BrokenEvents:
        def emit(self, *args, **kwargs):
            raise RuntimeError("timeline down")
    svc = WindowsTools(tools, profile=profile, state_dir=tmp_path, events=BrokenEvents(), executor=runner)
    assert svc.call(request())["reason"] == "timeline_unavailable"
    assert runner.calls == []
    assert svc.event_failures == 2
    svc._events = FakeEvents()
    assert svc.call(request())["status"] == "succeeded"


def test_t90_partial_state_marker_is_conservatively_uncertain(setup, tmp_path):
    *_, service = setup
    directory = tmp_path / "slots"
    directory.mkdir()
    (directory / "remote-device-b.lock").write_text("unc", encoding="utf-8")
    assert service().call(request())["reason"] == "reconciliation_required"


def test_t90_unimplemented_run_has_no_action_and_releases_slot(setup):
    _, _, runner, _, service = setup
    runner.run_error = NotImplementedError("transport missing")
    assert service().call(request())["reason"] == "not_implemented"
    runner.run_error = None
    assert service().call(request())["status"] == "succeeded"


def test_t90_slot_is_exclusive_across_processes(setup, tmp_path):
    import fcntl
    import multiprocessing
    _, _, runner, _, service = setup
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    def occupy():
        directory = tmp_path / "slots"
        directory.mkdir(exist_ok=True)
        with (directory / "remote-device-b.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            entered.set()
            release.wait(5)
    child = context.Process(target=occupy)
    child.start()
    try:
        assert entered.wait(5)
        assert service().call(request())["reason"] == "slot_busy"
        assert runner.calls == []
    finally:
        release.set()
        child.join(5)
    assert child.exitcode == 0
    assert service().call(request())["status"] == "succeeded"


def test_t90_standard_event_sink_queues_safe_events_offline(setup, tmp_path):
    from axcommon.events import EventSink
    tools, profile, runner, _, _ = setup
    sink = EventSink(None, "resources", "fixture-controller", tmp_path / "outbox")
    svc = WindowsTools(tools, profile=profile, state_dir=tmp_path / "slots", events=sink, executor=runner)
    assert svc.call(request())["status"] == "succeeded"
    entries = sink.outbox.read_all()
    assert len(entries) == 2
    assert all(item["body"]["kind"] == "tool.windows_call" for item in entries)
    assert [item["body"]["data"]["status"] for item in entries] == ["running", "succeeded"]
    for item in entries:
        schemas.validate("event", item["body"])


def test_t90_no_data_or_location_from_request_can_replace_trusted_metadata(setup):
    _, _, runner, _, service = setup
    assert service().call(request(location="caller-controlled"))["status"] == "rejected"
    assert runner.calls == []


@pytest.mark.parametrize("model", ["unknown-family", {"data_classes": ["sensitive"], "tier": []}])
def test_t90_malformed_family_metadata_cannot_widen_classes(setup, tmp_path, model):
    tools, profile, runner, events, _ = setup
    tools[1]["data_classes"] = ["public", "private", "sensitive"]
    runner.states["remote-device-b"] = DesktopState(True, True, False, model)
    svc = WindowsTools(tools, profile=profile, state_dir=tmp_path, events=events, executor=runner)
    assert svc.call(request(sensitivity="sensitive"))["reason"] == "data_class_denied"
    assert not runner.calls

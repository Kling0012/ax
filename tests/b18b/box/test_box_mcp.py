"""Offline driver tests. Shared-adapter integration is deliberately not claimed."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.resources.box.box_mcp import (
    ALLOWED_TOOLS, CAP_STAGE1_BASIS, CAP_TOOLS, DENIED_TOOLS, PROBE_CALLS,
    PROBE_IN_BOX_CALLS, BoxBindings, BoxMCPDriver, RUNNER_VERSION,
)
from core.resources.box.protocol import BoxError, IntegrationRequired, ToolDenied
from tests.fakes.box_fake import BoxFake, FakeClock, png
from tests.fakes.b18b.box.bindings import AX_KEY, WORKER_KEY, bindings


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AX_PUBLISH_LEGACY_PLUGIN_IDS", "legacy-publisher")
    clock = FakeClock()
    fake = BoxFake(root=tmp_path / "workspace", clock=clock)
    env = {"id": "box", "contract": 1, "mcp_url": "https://box.invalid/mcp", "runner": {"root": "/workspace/ax"},
           "profile": {"box_machine_name": "main", "box_machine_id": ""}, "limits": {"call_timeout_s": 120, "ready_wait_s": 120}}
    events = []
    binding = bindings(fake)
    driver = BoxMCPDriver(env, transport=fake, bindings=binding, emit=lambda kind, value: events.append((kind, copy.deepcopy(value))), clock=clock, sleep=clock.sleep)
    return driver, fake, binding, events, clock


def assert_code(code, function, *args, **kwargs):
    with pytest.raises(BoxError) as error:
        function(*args, **kwargs)
    assert error.value.code == code
    return error.value


def test_t181_probe_whitelist_and_no_storage(setup):
    driver, fake, _, events, _ = setup
    result = driver.probe()
    assert result["free_ok"] and result["machine_state"] == "hibernated"
    assert set(fake.names()) <= PROBE_CALLS
    for name in ("box_list", "box_status", "usage", "billing"):
        assert fake.count(name) == 1
    assert not (set(fake.names()) & DENIED_TOOLS)
    assert fake.count("box_wake") == 0
    with driver._scope("probe"):
        assert_code("tool_denied", driver.call_tool, "box_snapshots", {})
        assert_code("tool_denied", driver.call_tool, "shell", {"command": "anything"})
    assert any(kind == "env.usage" for kind, _ in events)


@pytest.mark.parametrize("sse", [False, True])
def test_mcp_json_sse_sessions_and_single_404_recovery(setup, sse):
    driver, fake, _, _, _ = setup
    fake.sse = sse
    driver.open_session()
    assert fake.count("initialize") == 1 and fake.initialized_notifications == 1
    fake.expire_next = True
    driver.list_tools()
    assert fake.count("initialize") == 2 and fake.initialized_notifications == 2
    fake.inject("tools/list", 404).inject("tools/list", 404)
    assert_code("session_expired", driver.list_tools)
    assert fake.count("initialize") == 3


@pytest.mark.parametrize("failure", ["credential_missing", "credential_insecure", "credential_invalid"])
def test_t128_t175_bad_keys_never_reach_mcp(setup, failure):
    driver, fake, binding, events, _ = setup
    binding.policy.key_error = failure
    result = driver.probe()
    assert result["error"] == failure
    assert not fake.calls
    assert result["failure"]["scope"] == "J"
    assert set(binding.policy.key_calls) == {("box", "ax")}
    assert AX_KEY not in json.dumps(events)
    assert AX_KEY not in repr(driver.session)


@pytest.mark.parametrize("value", ["", "x", "Bearer " + "x" * 32, "x y" * 10, "x\n" * 20, "é" * 20, "x" * 513])
def test_t128_invalid_key_defense_in_depth(setup, value):
    driver, fake, binding, _, _ = setup
    binding.policy.key = value
    assert driver.probe()["error"] == "credential_invalid"
    assert fake.calls == []


def test_t128_t173_revocation_affects_ax_only_and_key_is_reread(setup):
    driver, fake, binding, events, clock = setup
    assert driver.probe()["free_ok"]
    fake.revoked_keys.add(AX_KEY)
    result = driver.probe()
    assert result["error"] == "auth_failed" and result["failure"]["scope"] == "J"
    assert fake.count("initialize") == 1  # no auth retry
    # Fake transport verifies independent worker auth without loading a worker key in the driver.
    message = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {"protocolVersion": "2025-03-26"}}
    assert fake("POST", "https://box.invalid/mcp", {"Authorization": "Bearer " + WORKER_KEY}, json.dumps(message).encode(), 30)[0] == 200
    binding.policy.key = "r" * 32
    assert driver.probe()["free_ok"]
    for secret in (AX_KEY, WORKER_KEY, "r" * 32):
        assert secret not in json.dumps(events)
        assert secret not in repr(driver.session)


@pytest.mark.parametrize("status, code, state, scope", [(401, "auth_failed", "excluded", "J"), (403, "auth_failed", "excluded", "J"),
    (402, "quota", "excluded", "provider"), (429, "rate_limited", None, "provider"), (500, "unavailable", "excluded", "provider")])
def test_t170_t173_http_failure_codes(setup, status, code, state, scope):
    driver, fake, _, _, _ = setup
    fake.inject("initialize", status)
    result = driver.probe()
    assert result["error"] == code
    assert result["failure"]["env_state"] == state
    assert result["failure"]["scope"] == scope
    assert fake.count("initialize") == 1
    if status == 429:
        assert result["failure"]["job_state"] == "queued"
        assert result["failure"]["retry_after"] == 60


@pytest.mark.parametrize("error, code", [(ConnectionRefusedError("no"), "unavailable"), (TimeoutError("fake"), "timed_out")])
def test_t170_connection_failures_are_local(setup, error, code):
    driver, fake, _, _, _ = setup
    fake.inject("box_list", error)
    assert driver.probe()["error"] == code
    assert driver.failure("auth_failed")["card"]
    assert driver.failure("auth_failed")["card"] is False
    assert driver.probe()["free_ok"]  # unrelated subsequent activity can progress


def test_t176_constants_match_normative_table():
    expected = {
        "linux-sandbox": {"shell", "read_file", "write_file"}, "web-screenshot": {"browser_navigate", "screenshot"},
        "web-screenshot-full": {"shell"}, "web-gui": {"browser_navigate", "browser_click", "browser_type", "browser_snapshot"},
        "sandbox-desktop": {"computer_act", "computer_state", "gui_plan"}, "scheduled-publisher-cli": {"shell"}, "docker": {"shell"}}
    assert CAP_TOOLS == expected
    assert {cap for cap, value in CAP_STAGE1_BASIS.items() if not value} == {"web-screenshot-full", "scheduled-publisher-cli", "docker"}
    # Read the source table mechanically when the build design is available.
    spec = Path(__file__).resolve().parents[4] / "final" / "SPEC.md"
    if spec.exists():
        text = spec.read_text()
        section = text.split("#### S-25.31.2 ", 1)[1].split("#### S-25.31.3", 1)[0]
        import re
        rows = [line.split("|") for line in section.splitlines() if line.startswith("| `")]
        parsed = {re.findall(r"`([^`]+)`", row[1])[0]: set(re.findall(r"`([^`]+)`", row[2])) for row in rows}
        assert CAP_TOOLS == parsed


@pytest.mark.parametrize("removed, impacted", [("read_file", "linux-sandbox"), ("write_file", "linux-sandbox"),
                                              ("browser_snapshot", "web-gui"), ("gui_plan", "sandbox-desktop")])
def test_t176_one_missing_tool_only_removes_its_capability(setup, removed, impacted):
    driver, fake, _, _, _ = setup
    fake.tools.remove(removed)
    result = driver.probe()
    assert result["capabilities_checked"][impacted] == "unverified"
    assert result["capabilities_checked"]["web-screenshot"] == "ok"


def test_t176_absent_shell_overrides_previous_stage2(setup):
    driver, fake, _, _, _ = setup
    fake.state = "running"
    driver.probe()
    fake.state = "hibernated"
    fake.tools.remove("shell")
    result = driver.probe()
    for cap in ("linux-sandbox", "web-screenshot-full", "scheduled-publisher-cli", "docker"):
        assert result["capabilities_checked"][cap] == "unverified"


def test_t176_stage1_has_no_evidence_for_shell_only_capabilities(setup):
    driver, _, _, _, _ = setup
    result = driver.probe()
    assert all(result["capabilities_checked"][cap] == "unverified" for cap in ("web-screenshot-full", "scheduled-publisher-cli", "docker"))


def test_t176_failed_tools_list_keeps_previous_results_while_asleep(setup):
    driver, fake, _, _, _ = setup
    before = driver.probe()["capabilities_checked"]
    fake.inject("tools/list", {"tools": None})
    after = driver.probe()["capabilities_checked"]
    assert before == after


@pytest.mark.parametrize("allowed", [ALLOWED_TOOLS - {"read_file"}, ALLOWED_TOOLS | {"box_ensure"},
                                     ALLOWED_TOOLS | {"unknown_tool"}])
def test_t176_unsafe_table_disables_only_this_driver(setup, allowed):
    driver, fake, binding, _, clock = setup
    broken = BoxMCPDriver(driver.env, transport=fake, bindings=binding, allowed_tools=allowed)
    assert broken.probe()["error"] == "driver_disabled"
    assert fake.calls == []
    assert driver.probe()["free_ok"]


@pytest.mark.parametrize("cli, identity, expected", [(False, False, "unverified"), (True, False, "unverified"), (True, True, "ok")])
def test_t177_scheduled_publisher_measured_only_with_declared_slot(setup, cli, identity, expected):
    driver, fake, binding, _, _ = setup
    fake.state, fake.eigen_cli, fake.eigen_id = "running", cli, identity
    driver.env["detached_survival_verified"] = True
    driver.env["work_credentials"] = [{"slot": "scheduled-publisher-identity", "place": "box:/workspace/identity/.scheduled-publisher"}]
    result = driver.probe()
    assert result["capabilities_checked"]["scheduled-publisher-cli"] == expected
    assert binding.runner.selfcheck_paths == [("/workspace/bin/scheduled-publisher", "/workspace/identity/.scheduled-publisher")]
    assert fake.count("read_file") == fake.count("write_file") == 0
    driver.env["work_credentials"] = []
    assert driver.probe()["capabilities_checked"]["scheduled-publisher-cli"] == "unverified"


def test_t178_first_running_probe_and_repeat_gui_cadence(setup):
    driver, fake, _, events, clock = setup
    fake.state = "running"
    result = driver.probe()
    assert result["runner"] == "ok"
    assert fake.count("shell") == 2
    assert fake.count("screenshot") == fake.count("computer_state") == fake.count("browser_snapshot") == 1
    assert [call.args for call in fake.calls if call.name == "browser_navigate"] == [{"url": "https://example.com"}, {"url": "about:blank"}]
    fake.clear_calls()
    assert driver.probe()["capabilities_checked"]["web-gui"] == "ok"
    assert fake.count("browser_navigate") == fake.count("browser_snapshot") == 0
    assert fake.count("screenshot") == fake.count("computer_state") == 1
    clock.advance(24 * 3600)
    driver.probe()
    assert fake.count("browser_snapshot") == 1
    fake.state = "hibernated"
    driver.probe()
    fake.state = "running"
    driver.probe()
    assert fake.count("browser_snapshot") == 2
    driver.env["trial_for"] = "box"
    driver.probe()
    assert fake.count("browser_snapshot") == 3
    assert "https://" not in json.dumps(events)
    assert '"data"' not in json.dumps(events)
    assert not any(name in fake.names() for name in ("browser_click", "browser_type", "computer_act"))


@pytest.mark.parametrize("width,height,expected", [(1280, 800, "ok"), (1024, 640, "ok"), (800, 500, "unverified"), (1280, 720, "unverified")])
def test_t178_screenshot_dimensions(setup, width, height, expected):
    driver, fake, _, _, _ = setup
    fake.state = "running"
    fake.image = png(width, height)
    result = driver.probe()
    assert result["capabilities_checked"]["web-screenshot"] == expected
    assert result["capabilities_checked"]["sandbox-desktop"] == "ok"


def test_t178_invalid_image_and_missing_desktop_localized(setup):
    driver, fake, _, _, _ = setup
    fake.state, fake.image, fake.desktop_image = "running", b"not-a-png", None
    result = driver.probe()
    assert result["capabilities_checked"]["web-screenshot"] == "unverified"
    assert result["capabilities_checked"]["sandbox-desktop"] == "unverified"
    assert result["capabilities_checked"]["web-gui"] == "ok"


@pytest.mark.parametrize("fault", ["navigation", "snapshot"])
def test_t178_gui_failure_still_restores_blank_and_continues(setup, fault):
    driver, fake, _, _, _ = setup
    fake.state = "running"
    if fault == "navigation":
        fake.inject("browser_navigate", fake.error("fake"))
    else:
        fake.elements = []
    result = driver.probe()
    assert result["capabilities_checked"]["web-gui"] == "unverified"
    assert result["capabilities_checked"]["web-screenshot"] == "ok"
    assert [c.args["url"] for c in fake.calls if c.name == "browser_navigate"][-1] == "about:blank"


@pytest.mark.parametrize("case, expected", [("zero", "no_machine"), ("two", "machines_over_limit"), ("name", "free_unverified"),
                                           ("id", "free_unverified"), ("id_only", "free_unverified"), ("malformed", "free_unverified")])
def test_t179_machine_identity_failure_never_wakes(setup, case, expected):
    driver, fake, _, _, _ = setup
    if case == "zero": fake.machines = []
    if case == "two": fake.machines *= 2
    if case == "name": fake.machines[0]["name"] = "other"
    if case == "id": driver.env["profile"]["box_machine_id"] = "mismatch"
    if case == "id_only": driver.env["profile"].update(box_machine_name="", box_machine_id="fake-box-1")
    if case == "malformed": fake.inject("box_list", fake.result({"boxes": None}))
    result = driver.probe()
    assert result["error"] == expected
    assert fake.count("box_wake") == fake.count("box_ensure") == fake.count("box_create") == 0


def test_t179_empty_identity_accepts_exactly_one_machine(setup):
    driver, fake, _, _, _ = setup
    driver.env["profile"].update(box_machine_name="", box_machine_id="")
    fake.machines[0]["name"] = "any-name"
    assert driver.probe()["free_ok"]
    fake.machines *= 2
    assert driver.probe()["error"] == "machines_over_limit"


def test_t180_billing_read_is_probe_only_and_wallet_decrease_denies(setup):
    driver, fake, _, events, _ = setup
    assert_code("tool_denied", driver.call_tool, "billing", {})
    assert fake.count("billing") == 0
    fake.billing = fake.result({"plan_enabled": True, "included_boxes": ["main"], "wallet_balance": 100})
    assert driver.probe()["free_ok"]
    fake.billing = fake.result({"plan_enabled": True, "included_boxes": ["main"], "wallet_balance": 99})
    assert driver.probe()["error"] == "cost_nonzero"
    assert fake.count("billing") == 2
    assert "wallet_balance" not in json.dumps(events)
    with driver._scope("probe"):
        assert_code("tool_denied", driver.call_tool, "billing", {"action": "deposit"})
    assert fake.count("billing") == 2


@pytest.mark.parametrize("payload,code", [({"plan_enabled": True, "included_boxes": [], "wallet_balance": 0}, "not_free"),
    ({"unknown": True}, "free_unverified"), ({"plan_enabled": True, "included_boxes": ["main"], "wallet_balance": "0"}, "free_unverified")])
def test_t180_unverified_billing_shapes_fail_closed(setup, payload, code):
    driver, fake, _, _, _ = setup
    fake.billing = fake.result(payload)
    assert driver.probe()["error"] == code
    assert not set(fake.names()) & DENIED_TOOLS


@pytest.mark.parametrize("fault,code", [("cost", "cost_nonzero"), ("two", "machines_over_limit"), ("usage", "free_unverified")])
def test_t169_free_failure_emits_one_card_and_skips_stage2(setup, fault, code):
    driver, fake, _, events, _ = setup
    fake.state = "running"
    if fault == "cost": fake.cost_today = 1
    if fault == "two": fake.machines *= 2
    if fault == "usage": fake.inject("usage", fake.result({"bad": True})).inject("usage", fake.result({"bad": True}))
    first = driver.probe()
    second = driver.probe()
    assert first["error"] == second["error"] == code
    assert first["failure"]["card"] is True and second["failure"]["card"] is False
    assert first["failure"]["parked_card"] is False
    assert first["failure"]["exclude_min"] is None
    assert not set(fake.names()) & PROBE_IN_BOX_CALLS


def test_t169_disk_low_is_degraded_only(setup):
    driver, fake, _, _, _ = setup
    fake.state, fake.disk_free_mib = "running", 100
    result = driver.probe()
    assert result["free_ok"]
    assert result["failure"]["env_state"] == "degraded"
    assert result["failure"]["reason"] == "disk_full"


def test_t168_wake_once_and_poll_three_seconds(setup):
    driver, fake, _, _, clock = setup
    fake.wake_delay = 6
    driver.ensure_running()
    assert fake.count("box_wake") == 1
    names = [name for name in fake.names() if name.startswith("box_")]
    assert names[:3] == ["box_list", "box_status", "box_wake"]
    assert clock.now == 1800000006
    assert fake.state == "running"
    assert_code("tool_denied", driver.call_tool, "box_wake", {"box": "main", "wait": True})


def test_t168_wake_timeout_no_other_lifecycle_calls(setup):
    driver, fake, _, _, _ = setup
    fake.wake_delay = 1000
    assert_code("not_ready", driver.ensure_running)
    assert fake.count("box_wake") == 1
    assert not set(fake.names()) & DENIED_TOOLS


@pytest.mark.parametrize("param,value", [("idle", 1), ("tokens", 1), ("jobs", 1), ("queue", 1), ("busy", True), ("disabled", True)])
def test_t168_hibernate_requires_every_idle_condition(setup, param, value):
    driver, fake, _, _, _ = setup
    fake.state = "running"
    driver.env["sbx_idle_suspend_min"] = 5
    options = {"idle_s": 300, "queue_len": 0, "active_tokens": 0, "active_jobs": 0}
    if param == "idle": options["idle_s"] = value
    if param == "tokens": options["active_tokens"] = value
    if param == "jobs": options["active_jobs"] = value
    if param == "queue": options["queue_len"] = value
    if param == "busy": fake.busy = value
    if param == "disabled": driver.env["sbx_idle_suspend_min"] = 0
    assert driver.hibernate(**options) is False
    assert fake.count("box_hibernate") == 0


def test_t168_hibernate_then_new_job_wakes_again(setup):
    driver, fake, _, _, _ = setup
    driver.ensure_running()
    driver.env["sbx_idle_suspend_min"] = 5
    assert driver.hibernate(idle_s=300)
    assert fake.count("box_hibernate") == 1
    driver.ensure_running()
    assert fake.count("box_wake") == 2


@pytest.mark.parametrize("args", [{"action": "type_from_file"}, {"action": "TYPE_FROM_FILE"}, {"action": "click "}, {"action": "click", "path": "/a"},
    {"action": "type", "field": "number"}, {"actions": [{"action": "type_from_file"}]}, {"actions": "x"}, {"actions": [1]}, {"actions": [{}] * 51}, {}, []])
def test_t220_computer_args_denied_before_send(setup, args):
    driver, fake, binding, events, _ = setup
    assert_code("tool_denied", driver.call_tool, "computer_act", args)
    assert not fake.calls
    assert not binding.policy.scan_calls
    assert "type_from_file" not in json.dumps(events)
    assert all(item[1].get("deny_reason") == "arg_rule:computer_act" for item in events)


@pytest.mark.parametrize("args", [{"action": "type", "text": "type_from_file"}, {"actions": [{"action": "click", "x": 1}, {"action": "type", "text": "public text"}]}])
def test_t220_normal_action_and_literal_text_allowed(setup, args):
    driver, fake, _, _, _ = setup
    driver.call_tool("computer_act", args)
    assert fake.count("computer_act") == 1


def test_t220_unremovable_action_denial_only_disables_computer_act(setup):
    driver, fake, binding, _, _ = setup
    binding.policy.action_allow.add("type_from_file")
    assert_code("tool_denied", driver.call_tool, "computer_act", {"action": "click"})
    driver.call_tool("usage", {})
    assert fake.count("computer_act") == 0 and fake.count("usage") == 1


@pytest.mark.parametrize("name", sorted(DENIED_TOOLS | {"unknown_tool", "browser_unknown"}))
def test_t181_forbidden_names_cannot_be_reenabled(setup, name):
    driver, fake, _, _, _ = setup
    assert_code("tool_denied", driver.call_tool, name, {})
    assert fake.calls == []


@pytest.mark.parametrize("path", ["/workspace/ax", "/workspace/ax/../outside", "/workspace/ax-other/file", "relative", "/etc/file"])
def test_paths_are_validated_before_transport(setup, path):
    driver, fake, _, _, _ = setup
    assert_code("tool_denied", driver.call_tool, "write_file", {"path": path, "content": "public"})
    assert fake.calls == []


def test_outbound_scan_and_url_credentials_rejected(setup):
    driver, fake, binding, _, _ = setup
    assert_code("scan_blocked", driver.call_tool, "browser_type", {"text": binding.policy.blocked})
    assert_code("scan_blocked", driver.call_tool, "browser_navigate", {"url": "https://a:b@example.com"})
    assert_code("forbidden_command", driver.call_tool, "shell", {"command": "box connect"})
    assert fake.calls == []


def test_t170_shell_reset_rereads_status_before_finalizing(setup):
    driver, fake, _, _, _ = setup
    driver.ensure_running()
    fake.jobs["job-1"] = {"state": "running", "launched": True, "alive": True}
    fake.reset_on_shell = True
    assert driver.status(driver.env, "job-1")["state"] == "running"
    fake.child_survives_reset = False
    fake.reset_on_shell = True
    assert driver.status(driver.env, "job-1") == {"state": "failed", "reason": "session_reset"}
    fake.reset_on_shell = True
    assert driver.status(driver.env, "job-1", irreversible=True)["state"] == "lost"


def test_fetch_halves_truncated_chunk_and_scans_before_release(setup):
    driver, fake, binding, _, _ = setup
    driver.ensure_running()
    fake.artifacts[("job-1", "image.png")] = b"a" * 20000
    fake.truncate_fetch_above = 3072
    data = driver.fetch(driver.env, "job-1", "image.png")
    assert data == b"a" * 3072
    assert binding.policy.scan_calls[-1] == "inbound"
    fake.artifacts[("job-1", "bad.txt")] = binding.policy.blocked.encode()
    assert_code("artifact_scan_blocked", driver.fetch, driver.env, "job-1", "bad.txt")


def test_runner_contract_lookup_cancel_logs_and_install_failure(setup):
    driver, fake, _, _, _ = setup
    job = {"job_id": "job-1", "sensitivity": "public", "idempotency_key": "key-1", "entry": {"kind": "command", "args": {"cmd": "printf public"}}}
    driver.submit(driver.env, job)
    driver.submit(driver.env, job)
    assert len(fake.jobs) == 1
    assert driver.lookup(driver.env, "key-1")["job_id"] == "job-1"
    assert driver.logs(driver.env, "job-1")["stdout"] == "fake log"
    assert driver.cancel(driver.env, "job-1")["state"] == "cancelled"
    fake.install_failures = 3
    assert_code("runner_install_failed", driver.install_runner)
    assert driver.install_runner()["runner"] == "ok"


def test_put_file_fallback_uses_hdd_fake_shell_and_small_chunks(setup):
    driver, fake, _, _, _ = setup
    driver.ensure_running()
    fake.execute_shell, fake.write_limit = True, 10
    text = "public text\n" * 4000
    driver.put_file(driver.env, "/workspace/ax/jobs/job-1/spec.json", text)
    assert fake._file("/workspace/ax/jobs/job-1/spec.json").read_text() == text
    chunks = [call for call in fake.calls if call.name == "shell" and "base64 -d" in call.args["command"]]
    assert len(chunks) >= 3
    assert max(call.args_bytes for call in chunks) < 18000


def test_timeout_bounds_and_disk_full_do_not_retry_with_fallback(setup):
    driver, fake, _, _, _ = setup
    assert_code("timeout_out_of_range", driver.call_tool, "usage", {}, 121)
    assert fake.calls == []
    fake.disk_full = True
    assert_code("disk_full", driver.put_file, driver.env, "/workspace/ax/jobs/job-1/spec.json", "public")
    assert fake.count("shell") == 0


def test_missing_shared_assembly_is_explicit(setup):
    driver, fake, binding, _, _ = setup
    driver.bindings = BoxBindings(binding.policy, binding.guard)
    with pytest.raises(IntegrationRequired):
        driver.submit(driver.env, {"sensitivity": "public", "entry": {"kind": "command"}})


@pytest.mark.realenv
@pytest.mark.parametrize("test_id", ["T-140", "T-163", "T-167", "T-171"])
def test_real_box_not_run_in_build(test_id):
    pytest.skip(test_id + ": 実機の鍵と通信が必要。成果物づくりの間は実機を使わない。")

from pathlib import Path
import copy
import hashlib
import ipaddress
import json

import pytest

from axcommon import schemas
from core.resources.screenshot.entry_expand import (
    EXPAND, RUN, FETCH_BYTES, PNG_SIGNATURE, ScreenshotError, ScreenshotExecutor,
    image_from_response, inspect_png, load_table, prepare_entry, save_artifacts,
    select_implementation, validate_steps,
)
from tests.fakes.b18b.screenshot.public_page import FakeClock, FakeRunner, Policy, PublicPageDriver, png_bytes


def request(**args):
    return {"job_id": "fixture-job", "requires": ["web-screenshot"], "sensitivity": "public",
            "secrets": [], "entry": {"kind": "entrypoint", "name": "screenshot",
                                       "args": {"url": "https://example.com/", "wait_s": 0, **args}}}


def environment(source="box"):
    return {"id": source, "free_command": True, "entrypoints": {"screenshot": [RUN, EXPAND]},
            "limits": {"call_timeout_s": 120}}


def probe(runner="ok", full="ok", shot="ok"):
    return {"capabilities_checked": {"ax-runner": runner, "web-screenshot-full": full, "web-screenshot": shot}}


def plan_for(req=None, env=None, checked=None, tools=None, policy=None, **kwargs):
    policy = policy or Policy()
    return prepare_entry(env or environment(), req or request(), checked or probe(),
                         {"browser_navigate", "screenshot"} if tools is None else tools,
                         check_tool=policy.check_tool, scan_outbound=policy.scan_outbound, **kwargs)


def executor(policy, clock, driver, **kwargs):
    return ScreenshotExecutor(check_tool=policy.check_tool, scan_outbound=policy.scan_outbound,
                              scan_artifact=policy.scan_artifact, close_owned_tab=driver.close_owned_tab,
                              clock=clock, wait=clock.wait, utcnow=lambda: "2026-11-07T00:00:00Z", **kwargs)


@pytest.mark.parametrize("source", ["box", "instabox"])
@pytest.mark.parametrize("full_page", [True, False])
def test_t138_run_collection_and_metadata(tmp_path, source, full_page):
    policy, clock = Policy(), FakeClock()
    driver = PublicPageDriver(clock)
    runner = FakeRunner()
    plan = plan_for(request(full_page=full_page), environment(source), policy=policy)
    data, meta = executor(policy, clock, driver).execute(plan, runner=runner)
    manifest = save_artifacts(tmp_path, "job-one", data, meta, scan_artifact=policy.scan_artifact)
    assert data[:8] == PNG_SIGNATURE
    assert meta["source"] == source and meta["impl"] == "run"
    assert meta["mode"] == ("fullpage" if full_page else "viewport")
    assert meta["title"] and meta["http_status"] == 200
    assert manifest[0] == {"name": "page.png", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    assert driver.calls == []
    assert all(call[0].startswith("shell:") for call in runner.calls)
    assert all(call[3] <= FETCH_BYTES for call in runner.calls if call[0] == "shell:fetch")
    with pytest.raises(ScreenshotError, match="artifact_exists"):
        save_artifacts(tmp_path, "job-one", data, meta, scan_artifact=policy.scan_artifact)
    assert (tmp_path / "job-one/page.png").read_bytes() == data


@pytest.mark.parametrize("url", [
    "https://user:pass@example.com/", "http://localhost/", "http://127.0.0.1/",
    "http://[::1]/", "http://192.0.2.1/", "http://172.16.0.1/", "http://203.0.113.129/",
    f"http://{ipaddress.ip_network('100.64.0.0/10')[3001]}/", "https://host." + "ts.net/", "https://host.internal/", "https://host.local/",
    "https://example.com/?token=redacted", "https://example.com/?api_key=x",
    "https://example.com/?%61uth=x", "https://example.com/?%2574oken=x",
    "https://example.com:8080/", "file:///etc/hosts", "ftp://example.com/",
    "http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/", "http://127.1/",
    "http://[::ffff:127.0.0.1]/", "http://localhost./", "https://a.localhost/",
    "https://example.com\\@localhost/", "https://example.com/\nX", "http://host/",
    "http://169.254.169.254/", "http://0.0.0.0/", "http://[fc00::1]/",
])
@pytest.mark.parametrize("checked", [probe(), probe(runner="unverified")])
def test_t138_t148_urls_rejected_before_any_call(url, checked):
    driver = PublicPageDriver(FakeClock())
    with pytest.raises(ScreenshotError, match="url_denied"):
        plan_for(request(url=url), checked=checked)
    assert driver.calls == []


@pytest.mark.parametrize("overrides", [{"width": 319}, {"width": 2561}, {"height": 1601}, {"height": 319},
                                         {"wait_s": -1}, {"wait_s": 31}, {"full_page": 1}, {"headed": "true"},
                                         {"width": True}, {"steps": [{"tool": "box_delete"}]}])
def test_t138_strict_args(overrides):
    with pytest.raises(ScreenshotError, match="invalid_args"):
        plan_for(request(**overrides))


@pytest.mark.parametrize("sensitivity", ["private", "sensitive", None])
def test_t138_public_only(sensitivity):
    req = request()
    req["sensitivity"] = sensitivity
    with pytest.raises(ScreenshotError, match="sensitivity_denied"):
        plan_for(req)


def test_t138_secrets_and_nonpublic_inputs_go_through_shared_scan():
    req = request()
    req["secrets"] = ["slot-name"]
    with pytest.raises(ScreenshotError, match="sensitivity_denied"):
        plan_for(req)
    policy = Policy()
    req = request(url="https://example.com/" + policy.secret_marker)
    with pytest.raises(ScreenshotError, match="secret_detected"):
        plan_for(req, policy=policy)
    assert policy.tools == []


@pytest.mark.parametrize("failure, reason", [("short_read", "bad_png"), ("bad_hash", "bad_png"),
                                              ("bad_png", "bad_png")])
def test_t138_t185_run_failure_has_no_expand_retry(failure, reason):
    policy, clock = Policy(), FakeClock()
    driver = PublicPageDriver(clock)
    runner = FakeRunner(image=b"not PNG") if failure == "bad_png" else FakeRunner()
    if failure != "bad_png":
        setattr(runner, failure, True)
    with pytest.raises(ScreenshotError, match=reason) as err:
        executor(policy, clock, driver).execute(plan_for(), runner=runner)
    assert err.value.state == "failed"
    assert driver.calls == []


def test_t148_expand_order_metadata_and_owned_tab(tmp_path):
    policy, clock = Policy(), FakeClock()
    driver = PublicPageDriver(clock, png_bytes(640, 480))
    req = request(wait_s=2, full_page=True, width=1920)
    plan = plan_for(req, checked=probe(runner="unverified"), policy=policy)
    data, meta = executor(policy, clock, driver).execute(plan, driver=driver)
    assert [call[0] for call in driver.calls] == ["browser_navigate", "screenshot", "close_owned_tab"]
    assert clock.waits == [2] and driver.tabs == {"user-tab"}
    assert meta == {"final_url": "https://example.com/", "title": None, "http_status": None,
                    "width": 640, "height": 480, "captured_at": "2026-11-07T00:00:00Z",
                    "mode": "viewport", "source": "box", "impl": "expand",
                    "ignored_args": ["full_page", "width", "height", "headed"]}
    assert len(policy.tools) == 4  # preflight plus actual send
    assert save_artifacts(tmp_path, "expand", data, meta, scan_artifact=policy.scan_artifact)[0]["size"] > 0


@pytest.mark.parametrize("failure, reason", [("no_image", "no_image"), ("bad_png", "bad_png"), ("error", "tool_error")])
def test_t148_expand_failures_always_close_owned_tab(failure, reason):
    clock, policy = FakeClock(), Policy()
    driver = PublicPageDriver(clock, image=b"bad") if failure == "bad_png" else PublicPageDriver(clock)
    if failure != "bad_png":
        setattr(driver, failure, True)
    with pytest.raises(ScreenshotError, match=reason):
        executor(policy, clock, driver).execute(plan_for(checked=probe(runner="unverified")), driver=driver)
    assert driver.tabs == {"user-tab"}


def test_t148_limits_table_and_policy_preflight():
    with pytest.raises(ScreenshotError, match="steps_limit"):
        plan_for(checked=probe(runner="unverified"), sandbox_steps_max=1)
    assert plan_for(checked=probe(runner="unverified"), sandbox_steps_max=2).impl == "expand"
    with pytest.raises(ScreenshotError, match="steps_limit"):
        validate_steps([{"tool": "screenshot", "args": {}}] * 51,
                       check_tool=Policy().check_tool, scan_outbound=Policy().scan_outbound)
    table = load_table()
    table["screenshot"]["steps"][0]["tool"] = "box_delete"
    with pytest.raises(ScreenshotError, match="entry_table_invalid"):
        plan_for(checked=probe(runner="unverified"), table=table)
    policy = Policy()
    policy.deny = "screenshot"
    with pytest.raises(ScreenshotError, match="tool_denied"):
        plan_for(checked=probe(runner="unverified"), policy=policy)


@pytest.mark.parametrize("source", ["box", "instabox"])
@pytest.mark.parametrize("runner,full,shot,tools,want", [
    ("ok", "ok", "ok", {"browser_navigate", "screenshot"}, "run"),
    ("ok", "unverified", "ok", {"browser_navigate", "screenshot"}, "expand"),
    ("unverified", "ok", "ok", {"browser_navigate", "screenshot"}, "expand"),
    ("unverified", "ok", "ok", set(), None),
    ("ok", "unverified", "ok", {"screenshot"}, None),
    ("unverified", "unverified", "unverified", {"browser_navigate", "screenshot"}, None),
])
def test_t185_choice_depends_on_latest_probe_and_impl_not_provider(source, runner, full, shot, tools, want):
    result = select_implementation(environment(source), request(), probe(runner, full, shot), tools)
    assert result.impl == want
    if want is None:
        assert result.reason == "entry_unsupported"
        assert "no_usable_impl" in result.candidate_fields()["detail"]
    else:
        assert result.candidate_fields() == {"impl": want}


@pytest.mark.parametrize("runner,full", [("ok", "unverified"), ("unverified", "ok"), ("unverified", "unverified")])
def test_t148_t185_full_requirement_never_expands(runner, full):
    req = request()
    req["requires"] = ["web-screenshot-full"]
    choice = select_implementation(environment(), req, probe(runner, full), {"browser_navigate", "screenshot"})
    assert choice.impl is None and choice.reason == "entry_unsupported"


def test_t148_unknown_or_unregistered_entry():
    req = request()
    req["entry"]["name"] = "eigen-send"
    assert select_implementation(environment(), req, probe(), set()).reason == "entry_unsupported"
    env = environment()
    env["entrypoints"] = {}
    assert select_implementation(env, request(), probe(), set()).reason == "entry_unsupported"


def test_t185_registry_order_is_preserved():
    env = environment()
    env["entrypoints"]["screenshot"] = [EXPAND, RUN]
    assert select_implementation(env, request(), probe(), {"browser_navigate", "screenshot"}).impl == "expand"


def test_t185_running_plan_is_frozen_next_job_uses_new_probe():
    checked, req = probe(), request()
    plan = plan_for(req, checked=checked)
    runner, policy, clock = FakeRunner(), Policy(), FakeClock()
    driver = PublicPageDriver(clock)
    runner.on_start = lambda: checked["capabilities_checked"].update({"ax-runner": "unverified"})
    req["entry"]["args"]["url"] = "http://localhost/"
    _, meta = executor(policy, clock, driver).execute(plan, runner=runner)
    assert meta["impl"] == "run" and not driver.calls
    assert plan_for(checked=checked).impl == "expand"


@pytest.mark.parametrize("call_limit,total,expected", [(3, 60, 3), (3, 2, 2)])
def test_t185_instabox_impl_added_timeouts_are_provider_limits(call_limit, total, expected):
    policy, clock = Policy(), FakeClock()
    driver = PublicPageDriver(clock)
    driver.delay = 5
    env = environment("instabox")
    env["limits"]["call_timeout_s"] = call_limit
    plan = plan_for(request(wait_s=0), env, probe(runner="unverified"))
    with pytest.raises(ScreenshotError) as error:
        executor(policy, clock, driver, timeout_s=total).execute(plan, driver=driver)
    assert error.value.state == "timed_out"
    assert driver.calls[1][2] == expected
    assert driver.tabs == {"user-tab"}


def test_t185_instabox_normalized_image_and_no_image():
    policy, clock = Policy(), FakeClock()
    driver = PublicPageDriver(clock)
    plan = plan_for(env=environment("instabox"), checked=probe(runner="unverified"))
    _, meta = executor(policy, clock, driver).execute(plan, driver=driver)
    assert meta["source"] == "instabox" and meta["impl"] == "expand"
    driver.no_image = True
    with pytest.raises(ScreenshotError, match="no_image"):
        executor(policy, clock, driver).execute(plan, driver=driver)


def test_t148_total_timeout_accounts_for_adapter_wait():
    clock, policy = FakeClock(), Policy()
    driver = PublicPageDriver(clock)
    with pytest.raises(ScreenshotError, match="timed_out"):
        executor(policy, clock, driver, timeout_s=2).execute(
            plan_for(request(wait_s=3), checked=probe(runner="unverified")), driver=driver)
    assert [call[0] for call in driver.calls] == ["browser_navigate", "close_owned_tab"]


def test_t138_size_and_header_checks():
    data = png_bytes(320, 320)
    with pytest.raises(ScreenshotError, match="bad_png"):
        inspect_png(data, len(data) - 1)
    with pytest.raises(ScreenshotError, match="bad_png"):
        inspect_png(PNG_SIGNATURE)
    with pytest.raises(ScreenshotError, match="bad_png"):
        inspect_png(data[:29] + b"\0\0\0\0" + data[33:])
    with pytest.raises(ScreenshotError, match="bad_png"):
        image_from_response({"content": [{"type": "image", "mimeType": "image/png", "data": "!invalid!"}]})


def test_t138_secret_in_artifact_never_persisted(tmp_path):
    policy = Policy()
    with pytest.raises(ScreenshotError, match="secret_detected"):
        save_artifacts(tmp_path, "secret-job", png_bytes(), {"title": policy.secret_marker},
                       scan_artifact=policy.scan_artifact)
    assert not (tmp_path / "secret-job").exists()
    with pytest.raises(ScreenshotError, match="invalid_job_id"):
        save_artifacts(tmp_path, "../escape", png_bytes(), {}, scan_artifact=policy.scan_artifact)


def test_t138_only_screenshot_schemas_validated():
    from jsonschema import Draft202012Validator
    for name in ("args", "entry-steps", "meta"):
        Draft202012Validator.check_schema(schemas.load("ext/b18b/screenshot/" + name))
    assert schemas.is_valid("ext/b18b/screenshot/entry-steps", load_table())


@pytest.mark.parametrize("sse", [False, True])
def test_t148_box_driver_and_shared_mcp_fake(tmp_path, sse):
    from core.resources.box.box_mcp import BoxBindings, BoxMCPDriver
    from tests.fakes.b18b.screenshot.box_binding import BoxPolicy, ScreenshotBoxFake

    clock = FakeClock()
    fake = ScreenshotBoxFake(root=tmp_path, state="running", sse=sse, clock=clock)
    policy = BoxPolicy()
    env = {**environment(), "mcp_url": "https://box.invalid/mcp"}
    driver = BoxMCPDriver(env, transport=fake, bindings=BoxBindings(policy=policy, guard=None),
                          clock=clock, sleep=clock.wait)
    tools = set(driver.list_tools())
    fake.clear_calls()
    plan = plan_for(env=env, checked=probe(runner="unverified"), tools=tools, policy=policy)

    def close_owned_tab(response, timeout_s):
        assert response["owned_tab"] == "owned-tab"
        driver.call_tool("browser_tabs", {"fixture_close": response["owned_tab"]}, timeout_s)

    service = ScreenshotExecutor(check_tool=policy.check_tool, scan_outbound=policy.scan_outbound,
                                 scan_artifact=policy.scan_artifact, close_owned_tab=close_owned_tab,
                                 clock=clock, wait=clock.wait)
    data, meta = service.execute(plan, driver=driver)
    assert data[:8] == PNG_SIGNATURE and meta["impl"] == "expand"
    assert fake.names() == ["browser_navigate", "screenshot", "browser_tabs"]
    assert fake.tabs == {"user-tab"}
    assert fake.count("box_delete") == fake.count("box_create") == fake.count("shell") == 0


def test_t138_fetch_uses_multiple_bounded_chunks():
    policy, clock = Policy(), FakeClock()
    driver = PublicPageDriver(clock)
    runner = FakeRunner(png_bytes(2560, 1600))
    meta = json.loads(runner.payloads["out/meta.json"])
    meta.update(width=2560, height=1600)
    runner.payloads["out/meta.json"] = json.dumps(meta).encode()
    data, _ = executor(policy, clock, driver).execute(plan_for(), runner=runner)
    assert len(data) > FETCH_BYTES
    offsets = [call[2] for call in runner.calls if call[0] == "shell:fetch" and call[1] == "out/page.png"]
    assert offsets == list(range(0, len(data), FETCH_BYTES))


@pytest.mark.parametrize("gate", ["scan", "tool", "artifact"])
def test_t138_t148_false_policy_results_cannot_pass(gate, tmp_path):
    policy = Policy()
    if gate == "scan":
        with pytest.raises(ScreenshotError, match="secret_detected"):
            prepare_entry(environment(), request(), probe(), set(), scan_outbound=lambda _: False,
                          check_tool=policy.check_tool)
    elif gate == "tool":
        with pytest.raises(ScreenshotError, match="tool_denied"):
            prepare_entry(environment(), request(), probe(runner="unverified"), {"browser_navigate", "screenshot"},
                          scan_outbound=policy.scan_outbound, check_tool=lambda *_: False)
    else:
        with pytest.raises(ScreenshotError, match="secret_detected"):
            save_artifacts(tmp_path, "false-scan", png_bytes(), {}, scan_artifact=lambda _: False)
        assert not (tmp_path / "false-scan").exists()


@pytest.mark.parametrize("response", [{}, {"content": None}, {"content": "not-a-list"}, {"content": [None, "text"]}])
def test_t148_malformed_mcp_response_has_no_image(response):
    with pytest.raises(ScreenshotError, match="no_image"):
        image_from_response(response)


def test_t148_unmeasured_cleanup_fails_before_navigation():
    clock, policy = FakeClock(), Policy()
    driver = PublicPageDriver(clock)
    service = ScreenshotExecutor(check_tool=policy.check_tool, scan_outbound=policy.scan_outbound,
                                 scan_artifact=policy.scan_artifact, clock=clock, wait=clock.wait)
    with pytest.raises(NotImplementedError, match="F-21.2"):
        service.execute(plan_for(checked=probe(runner="unverified")), driver=driver)
    assert driver.calls == []


def test_t148_policy_change_after_selection_is_checked_before_send():
    clock, policy = FakeClock(), Policy()
    driver = PublicPageDriver(clock)
    plan = plan_for(checked=probe(runner="unverified"), policy=policy)
    policy.deny = "browser_navigate"
    with pytest.raises(ScreenshotError, match="tool_denied"):
        executor(policy, clock, driver).execute(plan, driver=driver)
    assert driver.calls == []


def test_t185_run_deadline_covers_collection():
    clock, policy = FakeClock(), Policy()
    runner = FakeRunner()
    runner.on_start = lambda: clock.wait(61)
    driver = PublicPageDriver(clock)
    with pytest.raises(ScreenshotError, match="timed_out"):
        executor(policy, clock, driver).execute(plan_for(), runner=runner)
    assert driver.calls == []
    assert len(runner.calls) == 1


@pytest.mark.parametrize("patch", [{"mode": "viewport"}, {"width": 42}, {"http_status": None},
                                    {"captured_at": "not-a-date"}, {"unexpected": "metadata"}])
def test_t138_run_metadata_is_checked(patch):
    clock, policy = FakeClock(), Policy()
    driver, runner = PublicPageDriver(clock), FakeRunner()
    # FakeRunner refreshes mode to mirror request; mutate its response after start.
    original = runner.run_entry

    def run_entry(args, timeout_s):
        original(args, timeout_s)
        meta = json.loads(runner.payloads["out/meta.json"])
        meta.update(patch)
        runner.payloads["out/meta.json"] = json.dumps(meta).encode()
        return {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in runner.payloads.items()}

    runner.run_entry = run_entry
    with pytest.raises(ScreenshotError, match="bad_metadata"):
        executor(policy, clock, driver).execute(plan_for(), runner=runner)
    assert driver.calls == []


@pytest.mark.realenv
@pytest.mark.skip(reason="T-131: real provider access and machine operations are outside this artifact-building task")
def test_t131_real_machine_screen_and_cleanup():
    """Real probe/free/one-machine, desktop and cleanup must be measured in B18."""

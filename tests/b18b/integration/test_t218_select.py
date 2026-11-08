"""T-218 J/G selection and gate-token handoff; all owners are in-process fakes."""
from copy import deepcopy

import pytest

from core.resources.ext.b18b import (
    EnvironmentSelection, GateChoice, IntegrationError, register,
    selection_authorize, selection_query,
)
from tests.fakes.b18b.integration.owners import GateOwner, box


def j(**kwargs):
    return {"requires": "linux-sandbox", "sensitivity": "public", **kwargs}


def g(**kwargs):
    return {"path": "G", "tools": "linux-sandbox", "sensitivity": "public", "role": "assist", **kwargs}


def selector(rows):
    owner = GateOwner(rows)
    service = EnvironmentSelection(snapshots=lambda: deepcopy(rows), emit=owner.emit,
                                   entry_supported=lambda env, request: env.get("free_command", False))
    return service, owner


def test_t218_a_default_is_j_and_uses_same_ranking_for_j_and_g():
    service, _ = selector([box(day_use_s=300), box("instabox", day_use_s=10)])
    assert service.select(j()) == service.select(j(path="J"))
    assert service.select(j())["selected"] == service.select(g())["selected"] == "instabox"
    with pytest.raises(IntegrationError) as error:
        service.select(j(path="X"))
    assert error.value.status == 422


@pytest.mark.parametrize("query", [g(tools=""), g(sensitivity=""), g(role=""),
                                    {"path": "G", "tools": "linux-sandbox", "sensitivity": "public"},
                                    g(requires="linux-sandbox"), g(record="1"), g(record="2"),
                                    g(needs_vision="maybe"), j(requires="")])
def test_t218_b_invalid_query_422_without_reading_registry(query):
    service = EnvironmentSelection(snapshots=lambda: pytest.fail("must not read registry"))
    with pytest.raises(IntegrationError) as error:
        service.select(query)
    assert error.value.status == 422


@pytest.mark.parametrize("invalidated,changed,unchanged,reason", [
    ("gate_state", g(), j(), "gate_state_not_available"),
    ("state", j(), g(), "state_not_available"),
])
def test_t218_c_revoked_key_excludes_only_its_path(invalidated, changed, unchanged, reason):
    rows = [box(**{invalidated: "excluded"}), box("instabox")]
    service, _ = selector(rows)
    result = service.select(changed)
    assert result["selected"] == "instabox"
    assert result["excluded"] == [{"env_id": "box", "reason": reason}]
    assert service.select(unchanged)["selected"] == "box"


def test_t218_d_unverified_capability_and_private_are_excluded():
    rows = [box(probe={"capabilities_checked": {"linux-sandbox": "unverified"}}), box("instabox")]
    service, _ = selector(rows)
    assert service.select(g())["excluded"] == [{"env_id": "box", "reason": "capability_unverified"}]
    result = service.select(g(sensitivity="private"))
    assert result["selected"] is None and not result["candidates"]
    assert result["excluded"][1]["reason"] == "sensitivity_denied"


def test_t218_e_get_does_not_acquire_slots_issue_does():
    service, owner = selector([box()])
    service.select(g())
    assert owner.env_slots == 0 and not owner.issued
    chooser = GateChoice(select=service.select, issue=owner.issue)
    assert chooser.acquire({"job_id": "parent-one", "role": "assist", "tools": ["linux-sandbox"],
                            "sensitivity": "public"})["env_id"] == "box"
    assert owner.env_slots == 1


def test_t218_f_event_allowlist_excludes_prompt_and_arbitrary_query_values():
    service, owner = selector([box(), box("instabox", slot_free=False)])
    service.select(g(record="0", prompt="untrusted-body", unused_field="never-log"))
    service.select(j(record="1", job_id="parent-one"))
    assert owner.events == []
    result = service.select(g(record="1", job_id="parent-one", prompt="untrusted-body", unused_field="never-log"))
    assert result["recorded"] is True and len(owner.events) == 1
    kind, data = owner.events[0]
    assert kind == "env.selection" and data["path"] == "G" and data["job_id"] == "parent-one"
    assert data["candidates"] and data["excluded"]
    assert set(data) == {"path", "job_id", "candidates", "excluded", "sensitivity"}
    assert "untrusted-body" not in str(data) and "never-log" not in str(data)


@pytest.mark.parametrize("path,side,credential,expected", [
    ("G", "core", "worker-pool", None), ("G", "work", "worker-pool", 403),
    ("G", "core", "plugin:test", 403), ("G", "work", "plugin:test", 403),
    ("G", "core", "hq", 403), ("J", "core", "worker-pool", 403),
    ("J", "core", "hq", None), ("J", "work", "plugin:test", None),
    ("G", "core", None, 401), ("J", "core", None, 401),
])
def test_t218_g_credentials_and_work_port(path, side, credential, expected):
    args = dict(path=path, side=side, credential=credential, query={},
                authorize_j=lambda who, query: who in ("hq", "plugin:test"))
    if expected is None:
        selection_authorize(**args)
    else:
        with pytest.raises(IntegrationError) as error:
            selection_authorize(**args)
        assert error.value.status == expected


def test_t218_h_trial_requires_matching_trial_for():
    service, _ = selector([box(gate_state="trial")])
    assert service.select(g())["excluded"][0]["reason"] == "trial_excluded"
    assert service.select(g(trial_for="other"))["selected"] is None
    assert service.select(g(trial_for="box"))["selected"] == "box"


def test_t218_i_slot_race_tries_next_candidate_then_deadline():
    service, owner = selector([box(), box("instabox")])
    owner.busy = {"box"}
    chooser = GateChoice(select=service.select, issue=owner.issue)
    request = {"job_id": "parent-one", "role": "assist", "tools": ["linux-sandbox"], "sensitivity": "public"}
    assert chooser.acquire(request)["env_id"] == "instabox"
    assert owner.issued == ["box", "instabox"]
    owner.busy = {"box", "instabox"}
    assert chooser.acquire(request) == {"state": "queued", "reason": "slot_busy"}
    assert chooser.acquire(request, deadline_expired=True) == {"state": "parked", "reason": "slot_busy"}
    service.snapshots = lambda: []
    owner.issued.clear()
    assert chooser.acquire(request)["state"] == "parked" and owner.issued == []


@pytest.mark.parametrize("kwargs,reason", [({"role": "impl"}, "role_denied"),
                                           ({"tools": "sandbox-desktop"}, "gate_not_ready"),
                                           ({"tools": "not-a-capability"}, "capability_unverified")])
def test_t218_g_gate_restrictions(kwargs, reason):
    service, _ = selector([box()])
    assert service.select(g(**kwargs))["excluded"][0]["reason"] == reason


def test_t218_j_screenshot_reuses_existing_entry_shape_selector():
    service, _ = selector([box()])
    result = service.select(j(requires="web-screenshot", entry_kind="entrypoint", entry_name="screenshot"))
    assert result["candidates"][0]["impl"] == "expand"
    assert service.select(j(requires="web-screenshot-full", entry_kind="entrypoint", entry_name="screenshot"))["selected"] is None


def test_t218_core_endpoint_logic_is_pure_and_authorized_before_selection():
    service, owner = selector([box()])
    with pytest.raises(IntegrationError) as error:
        selection_authorize(path="G", side="core", credential=None, authorize_j=lambda *_: False, query=g())
    assert error.value.status == 401
    with pytest.raises(IntegrationError) as error:
        selection_authorize(path="J", side="core", credential="worker-pool", authorize_j=lambda *_: False, query=j())
    assert error.value.status == 403
    selection_authorize(path="G", side="core", credential="worker-pool", authorize_j=lambda *_: False, query=g())
    result = service.select(g(record="1", job_id="parent-one"))
    assert result["path"] == "G" and len(owner.events) == 1 and owner.env_slots == 0


def test_t218_extension_registration_is_feature_local_without_http_dependency():
    from types import SimpleNamespace

    app = SimpleNamespace(state=SimpleNamespace(component="worker-pool"))
    assembly = register(app)
    assert register(app) is assembly
    with pytest.raises(NotImplementedError):
        app.state.ax_b18b.dispatch_worker()
    with pytest.raises(NotImplementedError):
        app.state.ax_b18b.box({"id": "box"})
    with pytest.raises(NotImplementedError):
        app.state.ax_b18b.call_windows({"tool_id": "explicit"})

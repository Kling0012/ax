"""T-103 offline registration preflight and integration assembly boundaries."""
from copy import deepcopy
from pathlib import Path

import pytest

from core.resources.ext.b18b import (
    B18bAssembly, IntegrationError, RelayRegistration, validate_relay_environment,
)
from tests.fakes.b18b.integration.owners import RouteOwner


def test_t103_candidate_is_confirmed_only_after_successful_probe():
    owner = RouteOwner()
    hq = deepcopy(owner.rows["hq-public"])
    registration = RelayRegistration(register_candidate=owner.register, probe=owner.probe, confirm=owner.confirm)
    route = {"id": "fixture-relay", "kind": "tunnel", "entry": "relay_entry_profile", "upstream": "relay_upstream_profile",
             "auth": "ssh-forced-command", "state": "candidate"}
    assert registration.register(route)["state"] == "candidate"
    assert [call[0] for call in owner.calls] == ["register", "probe"]
    owner.probe_ok = True
    assert registration.register(route)["state"] == "confirmed"
    assert owner.calls[-1] == ("confirm", "fixture-relay")
    assert owner.rows["hq-public"] == hq and route["state"] == "candidate"


@pytest.mark.parametrize("route", [{"id": "hq-public", "kind": "tunnel"},
                                    {"id": "new", "kind": "public-entry"},
                                    {"id": "new", "kind": "tunnel", "state": "confirmed"}])
def test_t103_never_changes_public_entry_or_trusts_claimed_confirmation(route):
    owner = RouteOwner()
    registration = RelayRegistration(register_candidate=owner.register, probe=owner.probe, confirm=owner.confirm)
    with pytest.raises(IntegrationError):
        registration.register(route)
    assert owner.calls == []


@pytest.mark.parametrize("row", [None, {"kind": "tunnel", "state": "candidate"},
                                  {"kind": "public-entry", "state": "confirmed"}])
def test_t103_missing_or_unconfirmed_relay_refuses_environment_registration(row):
    with pytest.raises(IntegrationError) as error:
        validate_relay_environment({"kind": "container", "transport": "relay", "relay_route": "unknown"},
                                   lookup_route=lambda route_id: row)
    assert error.value.code == "relay_route_unconfirmed"


def test_t103_confirmed_relay_passes_preflight_without_creating_a_port():
    calls = []
    validate_relay_environment({"adapter": "container", "transport": "relay", "relay_route": "fixture-relay"},
                               lookup_route=lambda route_id: calls.append(route_id) or {"kind": "tunnel", "state": "confirmed"})
    assert calls == ["fixture-relay"]


@pytest.mark.parametrize("field,value", [("kind", "windows-computer-use"), ("kind", "windows"),
                                          ("adapter", "windows-computer-use"), ("adapter", "windows")])
def test_t103_windows_never_registers_as_environment(field, value):
    with pytest.raises(IntegrationError):
        validate_relay_environment({field: value}, lookup_route=lambda route_id: pytest.fail("no lookup expected"))


def test_t103_unbound_route_owner_never_writes_routes_file():
    with pytest.raises(NotImplementedError):
        RelayRegistration().register({"id": "fixture-relay", "kind": "tunnel"})


def test_common_windows_delegates_to_explicit_tool_owner_only():
    class WindowsOwner:
        def call(self, request):
            assert request == {"tool_id": "explicit-target"}
            return {"state": "parked", "reason": "unavailable"}

    assembly = B18bAssembly()
    assembly.windows = WindowsOwner()
    assert assembly.call_windows({"tool_id": "explicit-target"})["reason"] == "unavailable"


def test_common_box_constructs_existing_driver_without_connecting():
    from core.resources.box.box_mcp import BoxBindings, BoxMCPDriver

    assembly = B18bAssembly()
    assembly.box_bindings = BoxBindings(policy=object(), guard=object())
    assembly.box_transport = lambda *args, **kwargs: pytest.fail("constructor must not call provider")
    driver = assembly.box({"id": "box", "mcp_url": "https://box.fixture.invalid/mcp", "limits": {"call_timeout_s": 120}})
    assert isinstance(driver, BoxMCPDriver)


def test_compose_overlay_is_only_resources_with_no_port_or_new_plugin():
    # worker-pool は caf546a がこの overlay に足した既存サービスへの env 拡張（新しい待受ではない）。
    # invariant は「b18b が resources + worker-pool の既存サービスだけを拡張し、新規の待ち受けや
    # 新規プラグインを増やさない」こと。services 集合全体が resources だけに限定される期待は廃止する。
    import yaml

    root = Path(__file__).resolve().parents[3]
    overlay = yaml.safe_load((root / "compose.d/b18b.yaml").read_text())
    assert set(overlay["services"]) == {"resources", "worker-pool"}
    for svc in ("resources", "worker-pool"):
        assert not overlay["services"][svc].get("ports")
    assert "networks" not in overlay and "secrets" not in overlay
    registry = yaml.safe_load((root / "plugins/registry.d/b18b.yaml").read_text())
    assert registry["plugins"] == []  # external tools are not plugins (S-25.1)

"""T-83 と T-116 の台帳試験。"""
from __future__ import annotations

from copy import deepcopy

import pytest

from core.resources.envs.registry import load_environment_file, load_environment_mapping


def _document(entry):
    return {"contract_supported": "1.0", "environments": [entry]}


@pytest.mark.parametrize("change", [
    lambda item: item.pop("cost"),
    lambda item: item.__setitem__("adapter", "unknown-adapter"),
    lambda item: item.__setitem__("capabilities", ["Bad_Capability"]),
    lambda item: item.__setitem__("work_credentials", [{"slot": "deploy", "value": "must-not-appear"}]),
    lambda item: item["conn_credential"].__setitem__("token", "must-not-appear"),
])
def test_t83_rejects_missing_or_unsafe_environment_entry(environment, change):
    entry = deepcopy(environment)
    change(entry)

    registry = load_environment_mapping(_document(entry))

    assert registry.environments == ()
    assert registry.rejected and registry.rejected[0][0] == "example-local"


def test_t83_keeps_valid_entries_when_another_entry_is_rejected(environment):
    invalid = deepcopy(environment)
    invalid["id"] = "bad-entry"
    invalid["adapter"] = "unknown-adapter"

    registry = load_environment_mapping({"contract_supported": ["1.0"], "environments": [environment, invalid]})

    assert [entry["id"] for entry in registry.environments] == ["example-local"]
    assert registry.rejected[0][0] == "bad-entry"


def test_t116_initial_registration_has_one_hub_host_local_and_only_credential_references(repo_root):
    registry = load_environment_file(repo_root / "config" / "environments.example.yaml")

    assert registry.rejected == ()
    assert [entry["id"] for entry in registry.environments] == ["hub-host-local", "legacy-host", "archive-host", "compute-host", "remote-host"]
    local = registry.get("hub-host-local")
    assert local is not None
    assert local["adapter"] == "ssh-host"
    assert local["connect"]["kind"] == "ssh"
    assert local["connect"]["host"] == "${AX_ENV_HUB_HOST_LOCAL_HOST}"
    assert local["conn_credential"] == {
        "slot": "env-ssh-hub-host-local",
        "place": "secrets_dir/env-ssh-hub-host-local",
    }
    assert all("value" not in credential for entry in registry.environments for credential in entry["work_credentials"])

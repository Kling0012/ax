"""T-84、T-98、T-100 の環境選択試験。"""
from __future__ import annotations

from copy import deepcopy

import pytest

from core.resources.envs.contracts import EnvironmentContractError, EnvironmentJob, SelectionRequest
from core.resources.envs.selection import select


def request(**changes):
    data = {"requires": ("ci-light",), "sensitivity": "public"}
    data.update(changes)
    return SelectionRequest(**data)


def test_t84_orders_by_capability_sensitivity_slot_then_cost(environment_copy):
    expensive = environment_copy(id="metered", cost={"kind": "metered"}, day_use_s=0)
    capability_limited = environment_copy(id="capability-limited", capabilities=["security-audit"], cost={"kind": "free"})
    busy = environment_copy(id="busy", cost={"kind": "free"}, slot_free=False)
    free = environment_copy(id="free", cost={"kind": "free"}, day_use_s=80)

    result = select([expensive, capability_limited, busy, free], request())

    assert result.selected == "free"
    assert result.excluded == (
        {"env_id": "capability-limited", "reason": "capability_unverified"},
        {"env_id": "busy", "reason": "slot_busy"},
    )


def test_t84_excludes_private_sandbox_owner_machine_and_rental_resident_output(environment_copy):
    sandbox = environment_copy(
        id="sandbox", adapter="cloud-sandbox", data_classes=["public"],
        probe={"capabilities_checked": {"ci-light": "ok"}},
    )
    owner = environment_copy(id="owner", owner_use=True)
    rental = environment_copy(id="rental", lifetime="rental")

    private = select([sandbox], request(sensitivity="private"))
    normal = select([owner], request())
    resident = select([rental], request(resident_output=True))

    assert private.selected is None and private.excluded[0]["reason"] == "sensitivity_denied"
    assert normal.selected is None and normal.excluded[0]["reason"] == "sensitivity_denied"
    assert resident.selected is None and resident.excluded[0]["reason"] == "resident_output_denied"
    assert select([owner], request(user_requested=True)).selected == "owner"


def test_t84_prefers_less_used_environment_after_equal_cost_and_rank(environment_copy):
    later = environment_copy(id="later", day_use_s=20, registered_at="2026-11-08")
    earlier = environment_copy(id="earlier", day_use_s=5, registered_at="2026-11-07")

    result = select([later, earlier], request(scope="each"))

    assert result.selected == "earlier"
    assert [candidate["env_id"] for candidate in result.candidates] == ["earlier", "later"]


def test_t98_trial_is_excluded_from_normal_selection_and_requires_matching_trial_for(environment_copy):
    trial = environment_copy(id="trial-env", state="trial")
    available = environment_copy(id="available")

    normal = select([trial, available], request())
    trial_job = select([trial, available], request(trial_for="trial-env"))

    assert normal.selected == "available"
    assert normal.excluded == ({"env_id": "trial-env", "reason": "trial_excluded"},)
    assert trial_job.selected == "trial-env"
    assert [candidate["env_id"] for candidate in trial_job.candidates] == ["trial-env", "available"]


@pytest.mark.parametrize("data", [
    {"job_id": "trial", "requires": ["ci-light"], "sensitivity": "private", "entry": {"kind": "entrypoint"}, "trial_for": "trial-env"},
    {"job_id": "trial", "requires": ["ci-light"], "sensitivity": "public", "entry": {"kind": "entrypoint"}, "trial_for": "trial-env", "secrets": ["deploy"]},
])
def test_t98_trial_job_is_public_and_has_no_secrets(data):
    with pytest.raises(EnvironmentContractError):
        EnvironmentJob.from_mapping(data)


def test_t100_uses_effective_classification_and_never_lowers_it(environment_copy):
    public = environment_copy(id="public", data_classes=["public"])
    private = environment_copy(id="private", data_classes=["public", "private"])
    narrowed = environment_copy(
        id="narrowed", data_classes=["public", "private"], effective_data_classes=["public"],
    )

    result = select([public, narrowed, private], request(input_sensitivities=("private",)))

    assert result.effective_sensitivity == "private"
    assert result.selected == "private"
    assert result.excluded == (
        {"env_id": "public", "reason": "sensitivity_denied"},
        {"env_id": "narrowed", "reason": "sensitivity_denied"},
    )


def test_t100_output_classification_can_only_narrow_selection(environment_copy):
    public = environment_copy(id="public", data_classes=["public"])
    sensitive = environment_copy(id="sensitive", data_classes=["public", "private", "sensitive"])

    result = select([public, sensitive], request(output_sensitivity="sensitive"))

    assert result.effective_sensitivity == "sensitive"
    assert result.selected == "sensitive"

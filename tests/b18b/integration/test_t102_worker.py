"""T-102: real parent jobs storage, with fake atomic/environment owner ports."""
import importlib
from dataclasses import replace

import pytest

from core.resources.ext.b18b import IntegrationError, WorkerBridge, WorkerLinks
from tests.fakes.b18b.integration.owners import Owner, model, worker_env


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AX_WORKER_LOCAL_ENV_ID", "hub-host-local")
    parents = importlib.import_module("plugins.resource.worker-pool.jobs").JobsStore(tmp_path / "parents.sqlite")
    links = WorkerLinks(tmp_path / "links.sqlite")
    owner = Owner()
    request = {"id": "parent-one", "role": "assist", "placement": "env", "sensitivity": "private",
               "verify": "python3 -m pytest tests/owned", "prompt": "work", "budget_min": 10}
    parents.create(request)
    bridge = WorkerBridge(parents=parents, links=links, ports=owner.ports())
    yield bridge, parents, links, owner, request
    parents.close()
    links.close()


def start(setup, **kwargs):
    bridge, _, _, _, request = setup
    return bridge.start("parent-one", request, models=kwargs.get("models", [model()]),
                        environments=kwargs.get("environments", [worker_env()]))


@pytest.mark.parametrize("placement", ["env", "any"])
def test_t102_child_is_worker_and_records_both_jobs(setup, placement):
    bridge, parents, links, owner, request = setup
    request["placement"] = placement
    result = start(setup)
    child = result["job_id"]
    assert links.get("parent-one")["child"] == child
    assert parents.get("parent-one")["state"] == "running"
    assert owner.children[child]["state"] == "running"
    assert (owner.model_used, owner.env_used) == (1, 1)
    _, env_id, spec = next(call for call in owner.calls if call[0] == "submit")
    assert env_id == "remote" and spec["entry"]["kind"] == "worker"
    assert spec["verify"]["cmd"] == request["verify"]
    assert spec["outputs"] == ["changes.bundle", "report.md"]
    assert not spec["irreversible"] and spec["secrets"] == []
    # A repeated call and a controller restart both reuse the durable child.
    start(setup)
    restarted = WorkerBridge(parents=parents, links=links, ports=owner.ports())
    assert restarted.start("parent-one", request, models=[model()], environments=[worker_env()])["job_id"] == child
    assert sum(call[0] == "submit" for call in owner.calls) == 1


@pytest.mark.parametrize("busy", ["model_cap", "env_cap"])
def test_t102_atomic_pair_rolls_back_partial_reservation(setup, busy):
    _, parents, _, owner, _ = setup
    setattr(owner, busy, 0)
    assert start(setup) == {"state": "queued", "reason": "slot_busy"}
    assert (owner.model_used, owner.env_used) == (0, 0)
    assert not any(call[0] == "submit" for call in owner.calls)
    assert parents.get("parent-one")["state"] == "queued"


@pytest.mark.parametrize("environment,models", [
    (worker_env(data_classes=["public"]), [model()]),
    (worker_env(model_family=["other"]), [model()]),
    (worker_env(worker={}), [model()]),
    (worker_env(state="excluded"), [model()]),
    (worker_env(), [model(runner="unknown")]),
    (worker_env(), [model(status="excluded")]),
    (worker_env(model_family=["claude"]), [model("claude/test", family="claude")]),
])
def test_t102_invalid_pairs_are_parked_without_local_fallback(setup, environment, models):
    _, _, _, owner, _ = setup
    assert start(setup, environments=[environment], models=models)["state"] == "parked"
    assert not owner.leases and not any(call[0] == "submit" for call in owner.calls)


@pytest.mark.parametrize("tier,expected", [("free", "parked"), ("flash", "parked"), ("standard", "running")])
def test_t102_sensitive_tier_policy_and_training_not_used(setup, tier, expected):
    setup[4]["sensitivity"] = "sensitive"
    assert start(setup, models=[model(tier=tier, trains_on_input=True)])["state"] == expected


def test_t102_isolation_failure_disables_only_worker_use(setup):
    _, _, _, owner, _ = setup
    owner.isolation_ok = False
    environment = worker_env()
    assert start(setup, environments=[environment])["state"] == "parked"
    assert environment["state"] == "available" and not owner.leases


@pytest.mark.parametrize("overrides", [{"irreversible": True}, {"secrets": ["publishing"]}])
def test_t102_worker_prohibited_requests_do_not_reach_environment(setup, overrides):
    setup[4].update(overrides)
    assert start(setup)["state"] == "rejected"
    assert not setup[3].calls


@pytest.mark.parametrize("rc,status,expected", [(0, "ready", "ok"), (1, "ready", "failed"),
                                               (None, "ready", "failed"), (False, "ready", "failed"),
                                               (0, "blocked", "failed")])
def test_t102_independent_environment_verify_decides_parent_result(setup, rc, status, expected):
    bridge, parents, _, owner, _ = setup
    child = start(setup)["job_id"]
    output = {"state": "succeeded", "verify": {"rc": rc},
              "report": {"status": status, "text": "worker claims success"}}
    result = bridge.complete("parent-one", output)
    assert result["parent_state"] == expected
    assert parents.get("parent-one")["state"] == expected
    assert owner.children[child]["result"] == output
    bridge.complete("parent-one", output)
    assert owner.releases == 1 and not owner.leases


@pytest.mark.parametrize("state", ["failed", "timed_out", "stalled", "cancelled", "rejected"])
def test_t102_terminal_failures_release_both_slots_once_and_leave_other_jobs(setup, state):
    bridge, parents, _, owner, request = setup
    parents.create({**request, "id": "unrelated", "placement": "local", "state": "running"})
    start(setup)
    result = {"state": state, "error": {"class": "internal", "message": "not persisted as reason"}}
    bridge.complete("parent-one", result)
    assert parents.get("unrelated")["state"] == "running"
    assert parents.get("parent-one")["reason"] == "internal"
    assert (owner.model_used, owner.env_used, owner.releases) == (0, 0, 1)


def test_t102_ambiguous_submit_never_retries_and_keeps_lease_until_lookup(setup):
    bridge, parents, links, owner, _ = setup
    owner.submit_error = TimeoutError("uncertain")
    assert start(setup)["state"] == "lost"
    assert links.get("parent-one")["released"] == 0 and owner.leases
    start(setup)
    assert sum(call[0] == "submit" for call in owner.calls) == 1
    bridge.complete("parent-one", {"state": "failed"})
    assert not owner.leases and parents.get("parent-one")["state"] == "failed"


def test_t102_scan_blocks_output_before_parent_or_child_storage(setup):
    bridge, parents, links, owner, _ = setup
    child = start(setup)["job_id"]
    bridge.complete("parent-one", {"state": "succeeded", "report": {"status": "ready", "text": "forbidden-output"},
                                   "verify": {"rc": 0}})
    assert parents.get("parent-one")["state"] == "failed"
    assert "forbidden-output" not in str(owner.children[child]) + str(links.get("parent-one"))


def test_t102_unscanned_running_output_keeps_uncertain_lease(setup):
    bridge, _, _, owner, _ = setup
    start(setup)
    assert bridge.complete("parent-one", {"state": "running", "summary": "forbidden-output"})["state"] == "lost"
    assert owner.leases


def test_t102_missing_verify_is_not_worker_claimed_success(setup):
    setup[4]["verify"] = ""
    with pytest.raises(IntegrationError):
        start(setup)
    assert not setup[3].calls


def test_t102_any_prefers_local_only_when_conditions_equal(setup):
    setup[4]["placement"] = "any"
    assert start(setup, environments=[worker_env("remote"), worker_env("hub-host-local")])["env_id"] == "hub-host-local"


def test_t102_cheapest_valid_model_precedes_environment_price(setup):
    remote = worker_env("remote", cost={"kind": "free"}, model_family=["other"])
    local = worker_env("hub-host-local", cost={"kind": "fixed"})
    models = [model("other/test", family="other", tier="last_resort"), model()]
    assert start(setup, models=models, environments=[remote, local])["env_id"] == "hub-host-local"

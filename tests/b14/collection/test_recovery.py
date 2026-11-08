"""Additional recovery boundaries for T-20 / T-21."""
import pytest
from plugins.subloop.change.collection import ItemKey, Observation, Snapshot, TargetRegistry
from .test_t20 import add, key
from .conftest import target


def test_unknown_running_work_keeps_conflicting_reservation(rig):
    store, registry, client, service, clock = rig
    first_key = add(rig)
    add(rig, number=2)
    other_key = add(rig, repo="owner/b")
    claim = store.claim_next("c1", registry, "worker", lease_seconds=2)
    store.record_submission(claim, "uncertain-job")
    clock.value += 3
    service.reconcile_external()
    assert store.get(first_key)["state"] == "lost"
    assert store.get(first_key)["owner"] == "worker"
    next_claim = service.claim_next("c1", "other-worker")
    assert next_claim["key"] == other_key.encode()
    # A later confirmed terminal observation releases the quarantine without relaunching.
    client.work["uncertain-job"] = Observation("completed", "uncertain-job")
    service.reconcile_external()
    assert store.get(first_key)["owner"] is None
    assert client.writes == []


def test_old_work_observation_cannot_revert_completed_item(rig):
    store, registry, client, service, _ = rig
    item = add(rig)
    claim = store.claim_next("c1", registry, "worker")
    store.record_submission(claim, "job")
    stale = store.operation(item, "work")
    store.finish(claim)
    store.reconcile_work(stale, Observation("running", "job"))
    assert store.get(item)["state"] == "completed"
    assert store.get(item)["owner"] is None


def test_checkpoint_cannot_claim_new_version_was_verified(rig):
    store, registry, *_ = rig
    item = add(rig)
    claim = store.claim_next("c1", registry, "worker")
    store.collect(Snapshot(item, "changed"), registry)
    with pytest.raises(ValueError, match="checkpoint_version_changed"):
        store.save_checkpoint(claim, verified_results=["old-evidence"], data={})


def test_retry_becomes_eligible_only_after_deadline(rig):
    store, registry, _, _, clock = rig
    item = add(rig)
    store.hold(item, "retry", reason="temporary", check_at="1970-01-01T00:16:45.000Z")
    assert store.claim_next("c1", registry, "worker") is None
    clock.value = 1005
    assert store.claim_next("c1", registry, "worker")["key"] == item.encode()
    assert store.get(item)["hold"] is None


def test_reject_incomplete_danger_path_configuration(rig):
    registry = TargetRegistry([target(untrusted_forbidden_paths=["src/**"]), target("owner/b")])
    assert set(registry.targets) == {"owner/b"}
    assert registry.errors == {"owner/a": "invalid_or_ambiguous_target"}


@pytest.mark.parametrize("seconds", [float("inf"), float("nan"), True])
def test_non_finite_cycle_budget_is_rejected(rig, seconds):
    store, *_ = rig
    with pytest.raises(ValueError):
        store.begin_cycle("invalid", change_cycle_seconds=seconds)

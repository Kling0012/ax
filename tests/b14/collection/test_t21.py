"""T-21: queue order, repository/path locks, cycle budgets and modes."""
import pytest
from plugins.subloop.change.collection import CollectionService, CollectionStore, ItemKey, Snapshot, TargetRegistry
from .conftest import target
from .test_t20 import add, key


def test_t21_priority_fifo_and_key_tie_break(rig):
    store, registry, _, _, clock = rig
    add(rig, number=8, labels=("normal",))
    clock.value += 1
    add(rig, number=4, labels=("urgent",))
    add(rig, number=2, labels=("urgent",))
    add(rig, number=1)
    assert [ItemKey.decode(row["key"]).number for row in store.waiting(registry)] == [2, 4, 8, 1]
    clock.value += 1
    add(rig, number=2, labels=("urgent",))
    assert [ItemKey.decode(row["key"]).number for row in store.waiting(registry)] == [2, 4, 8, 1]


def test_t21_same_repo_and_overlapping_paths_are_serialized(rig):
    store, registry, _, _, clock = rig
    a = add(rig)
    a2 = add(rig, number=2)
    b = add(rig, repo="owner/b")
    c = add(rig, repo="owner/c")
    paths = {a.encode(): ["shared/config/**"], a2.encode(): ["different/**"], b.encode(): ["shared/config/item"], c.encode(): ["independent/**"]}
    first = store.claim_next("c1", registry, "one", paths_by_key=paths, lease_seconds=5)
    assert first["key"] == a.encode()
    second = store.claim_next("c1", registry, "two", paths_by_key=paths, lease_seconds=5)
    assert second["key"] == c.encode()
    assert store.get(a2)["state"] == store.get(b)["state"] == "planned"
    assert store.get(b)["retry_at"] == clock.value + 5
    clock.value += 6
    assert store.claim_next("c1", registry, "three", paths_by_key=paths) is None
    # Expiry alone does not prove that the original work has stopped.
    with pytest.raises(ValueError, match="stale_lease"):
        store.finish(first)


def test_t21_unrelated_repositories_progress_and_fenced_release(rig):
    store, registry, *_ = rig
    a = add(rig)
    b = add(rig, repo="owner/b")
    first = store.claim_next("c1", registry, "worker-a")
    second = store.claim_next("c1", registry, "worker-b")
    assert first and second
    assert {first["key"], second["key"]} == {a.encode(), b.encode()}
    with pytest.raises(ValueError, match="stale_lease"):
        store.finish({**first, "lease_token": "wrong"})
    store.renew(first, lease_seconds=120)
    store.finish(first)
    assert store.get(a)["owner"] is None
    assert store.get(b)["owner"] == "worker-b"


def test_t21_target_cap_does_not_block_others(rig):
    store, _, _, _, _ = rig
    registry = TargetRegistry([target(execution_cap=0), target("owner/b")])
    add(rig)
    b = add(rig, repo="owner/b")
    claim = store.claim_next("c1", registry, "worker")
    assert claim["key"] == b.encode()
    assert store.get(key())["state"] == "planned"


def test_t21_start_budget_persists_across_restart_and_next_cycle(rig):
    store, registry, _, _, clock = rig
    a = add(rig)
    b = add(rig, repo="owner/b")
    store.begin_cycle("limited", change_starts_per_cycle=1)
    claim = store.claim_next("limited", registry, "worker")
    store.finish(claim)
    restarted = CollectionStore(store.path, clock=clock)
    restarted.begin_cycle("limited", change_starts_per_cycle=1)
    assert restarted.claim_next("limited", registry, "worker") is None
    assert restarted.get(b)["state"] == "planned"
    restarted.begin_cycle("next", change_starts_per_cycle=1)
    assert restarted.claim_next("next", registry, "worker")["key"] == b.encode()
    with pytest.raises(ValueError):
        restarted.begin_cycle("limited", change_starts_per_cycle=4)


def test_t21_model_budget_and_time_leave_waiting_items(rig):
    store, registry, _, _, clock = rig
    a = add(rig)
    store.begin_cycle("model", change_model_calls_per_cycle=2)
    assert store.consume_model_calls("model", 2)
    assert not store.consume_model_calls("model")
    assert store.claim_next("model", registry, "worker") is None
    store.begin_cycle("time", change_cycle_seconds=2)
    clock.value += 2
    assert not store.consume_model_calls("time")
    assert store.claim_next("time", registry, "worker") is None
    assert store.get(a)["state"] == "planned"


@pytest.mark.parametrize("mode,dry_run,collects", [("collect_only", False, True), ("run", True, True), ("poll_paused", False, False), ("draining", False, False)])
def test_t21_modes_never_start_or_write(rig, mode, dry_run, collects):
    store, registry, client, service, _ = rig
    item = key()
    client.items["owner/a"] = [Snapshot(item, "v1")]
    store.begin_cycle("mode", mode=mode, dry_run=dry_run)
    assert service.collect("mode")["discovered"] == int(collects)
    assert service.claim_next("mode", "worker") is None
    assert not store.consume_model_calls("mode")
    assert service.receipt("mode", item, "Accepted")["state"] == "skipped"
    if collects:
        assert service.sync_labels("mode", item, "planned")["state"] == "skipped"
        assert store.get(item)["state"] == "planned"
    assert client.writes == []
    assert store.cycle("mode")["starts"] == 0


def test_t21_label_diff_preserves_unrelated_labels(rig):
    store, registry, client, service, _ = rig
    item = add(rig)
    claim = store.claim_next("c1", registry, "worker")
    store.record_submission(claim, "job-1")
    from plugins.subloop.change.collection import Observation
    client.work["job-1"] = Observation("running", "job-1")
    service.reconcile_external()
    client.labels[item] = {"bug", "ax-waiting"}
    assert service.sync_labels("c1", item, "running")["state"] == "completed"
    assert client.labels[item] == {"bug", "ax-running"}
    count = len(client.writes)
    assert service.sync_labels("c1", item, "running")["changed"] is False
    assert len(client.writes) == count
    store.finish(claim)
    service.sync_labels("c1", item, "completed")
    assert client.labels[item] == {"bug", "ax-completed"}
    # Repair external drift even when the desired state is unchanged.
    client.labels[item] = {"bug"}
    service.sync_labels("c1", item, "completed")
    assert client.labels[item] == {"bug", "ax-completed"}


def test_t21_rank_updates_only_on_change(rig):
    _, _, client, service, _ = rig
    item = add(rig)
    service.receipt("c1", item, "Accepted")
    service.update_rank("c1", item, 1)
    assert len(client.writes) == 2
    assert service.update_rank("c1", item, 1)["changed"] is False
    assert len(client.writes) == 2
    assert service.update_rank("c1", item, 2)["state"] == "completed"
    assert len(client.writes) == 3


def test_t21_writes_require_registered_permission_and_outbound_check(rig):
    store, registry, client, _, _ = rig
    item = add(rig)
    denied = CollectionService(store, registry, client)
    assert denied.receipt("c1", item, "Accepted")["state"] == "skipped"
    assert denied.sync_labels("c1", item, "planned")["state"] == "skipped"
    assert client.writes == []
    assert store.operation(item, "receipt") is None

"""T-20: explicit targets, atomic deduplication, reconciliation and resumption."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from plugins.subloop.change.collection import CollectionService, CollectionStore, ItemKey, Observation, Snapshot, TargetRegistry
from .conftest import target


def key(number=1, repo="owner/a", kind="issue"):
    return ItemKey("github", repo, kind, number)


def add(rig, number=1, repo="owner/a", kind="issue", **kwargs):
    store, registry, *_ = rig
    item = key(number, repo, kind)
    assert store.collect(Snapshot(item, "v1", **kwargs), registry)
    return item


def test_t20_explicit_and_unambiguous_targets(rig):
    store, registry, client, service, _ = rig
    invalid = target("owner/c")
    invalid.pop("test_recipe")
    registry = TargetRegistry([target(), target("owner/a", "owner/other"), target("owner/b"), invalid])
    assert set(registry.targets) == {"owner/b"}
    assert set(registry.errors) == {"owner/a", "owner/c"}
    assert not store.collect(Snapshot(key(repo="owner/not-registered"), "v1"), registry)
    assert not store.collect(Snapshot(key(), "v1"), registry)
    assert store.collect(Snapshot(key(repo="owner/b"), "v1"), registry)


def test_t20_case_normalization_and_distinct_kinds(rig):
    store, *_ = rig
    add(rig)
    add(rig, kind="pr")
    add(rig, repo="owner/b")
    add(rig, repo="OWNER/A")
    assert len(store.rows()) == 3
    assert key() == key(repo="OWNER/A")
    assert ItemKey.decode(key().encode()) == key()


def test_t20_atomic_collection_and_start(rig):
    store, registry, _, _, clock = rig
    def collect(_):
        second = CollectionStore(store.path, clock=clock)
        return second.collect(Snapshot(key(), "v1"), registry)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(collect, range(16)))
    assert len(store.rows()) == 1
    def claim(n):
        second = CollectionStore(store.path, clock=clock)
        return second.claim_next("c1", registry, str(n))
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))
    assert sum(claim is not None for claim in claims) == 1
    assert store.cycle("c1")["starts"] == 1


def test_t20_preserves_acceptance_time_and_repo_separation(rig):
    store, registry, *_ = rig
    registry = TargetRegistry([target("owner/public", "owner/private")])
    item = key(repo="owner/public")
    assert store.collect(Snapshot(item, "v1", implementation_pr={"repo": "owner/private", "number": 90}), registry)
    before = store.get(item)["received_at"]
    assert store.collect(Snapshot(item, "v2"), registry)
    row = store.get(item)
    assert row["received_at"] == before
    assert row["implementation_repo"] == "owner/private"
    assert ItemKey.decode(row["key"]).issue_repo == "owner/public"
    changed = TargetRegistry([target("owner/public", "owner/new-private")])
    assert not store.collect(Snapshot(item, "v2"), changed)
    assert store.get(item)["hold"] == "parked"


def test_t20_checkpoint_reuses_only_verified_same_version(rig):
    store, registry, *_ = rig
    item = add(rig)
    claim = store.claim_next("c1", registry, "worker")
    store.save_checkpoint(claim, verified_results=["evidence:1"], data={"stage": "verified"})
    assert store.resume_checkpoint(item, version="v1", verified_results=["evidence:1"]) == {"stage": "verified"}
    assert store.resume_checkpoint(item, version="v1", verified_results=["evidence:2"]) is None
    assert store.resume_checkpoint(item, version="v2", verified_results=["evidence:1"]) is None
    store.collect(Snapshot(item, "v2"), registry)
    assert store.resume_checkpoint(item, version="v1", verified_results=["evidence:1"]) is None


def test_t20_receipt_posts_once_and_does_not_scan_legacy_posts(rig):
    store, registry, client, service, _ = rig
    item = add(rig)
    client.comments[item] = {"legacy": {"id": "legacy", "body": "old workflow receipt", "author": "owner"}}
    first = service.receipt("c1", item, "Accepted")
    assert first["state"] == "completed"
    second = CollectionService(CollectionStore(store.path, clock=store.clock), registry, client, permission_check=lambda *_: True, outbound_check=lambda _: True)
    assert second.receipt("c1", item, "Accepted")["external_id"] == first["external_id"]
    assert len(client.writes) == 1
    assert all(read[0] != "list_comments" for read in client.reads)
    assert first["external_id"] != "legacy"


def test_t20_uncertain_post_is_lost_and_never_resent(rig):
    store, registry, client, service, _ = rig
    item = add(rig)
    client.fail_after_post = True
    assert service.receipt("c1", item, "Accepted")["state"] == "lost"
    assert len(client.writes) == 1
    service.reconcile_external()
    client.fail_after_post = False
    assert service.receipt("c1", item, "Accepted")["state"] == "lost"
    assert len(client.writes) == 1
    assert store.get(item)["hold"] == "parked"
    assert service.claim_next("c1", "worker") is None


def test_t20_restart_reconciles_known_external_id(rig):
    store, registry, client, service, _ = rig
    item = add(rig)
    op = store.prepare_operation(item, "receipt", {"body": "hello marker", "marker": "marker"})
    cid = client.post_comment(item, "hello marker", "marker")
    store.update_operation(op, "submitted", cid)
    service.reconcile_external()
    assert store.operation(item, "receipt")["state"] == "completed"
    assert len(client.writes) == 1


def test_t20_restart_reconciles_running_work_and_expired_ownership(rig):
    store, registry, client, service, clock = rig
    item = add(rig)
    add(rig, number=2)
    add(rig, repo="owner/b")
    claim = store.claim_next("c1", registry, "worker", lease_seconds=5)
    store.record_submission(claim, "job-1")
    clock.value += 6
    client.work["job-1"] = Observation("running", "job-1")
    service.reconcile_external()
    assert store.get(item)["state"] == "running"
    assert store.get(item)["owner"] == "worker"
    assert store.get(item)["lease_until"] > clock.value
    next_claim = service.claim_next("c1", "worker-b")
    assert ItemKey.decode(next_claim["key"]).issue_repo == "owner/b"
    client.work["job-1"] = Observation("completed", "job-1")
    service.reconcile_external()
    assert store.get(item)["owner"] is None
    assert store.get(item)["state"] == "completed"


def test_t20_restart_launch_without_external_id_never_relaunches(rig):
    store, registry, _, service, _ = rig
    item = add(rig)
    store.claim_next("c1", registry, "worker")
    service.reconcile_external()
    assert store.get(item)["state"] == "lost"
    assert service.claim_next("c1", "worker2") is None
    with pytest.raises(ValueError):
        store.reopen(item, authorized=True)


def test_t20_comment_boundary_closed_edits_ack_and_same_author(rig):
    store, _, client, service, _ = rig
    item = add(rig, external_state="closed")
    receipt = service.receipt("c1", item, "Accepted")
    client.comments[item]["human"] = {"id": "human", "author": "example-owner", "body": "new input",
        "created_at": "2026-11-07T01:00:00.000Z", "revision": "r1"}
    client.comments[item]["old"] = {"id": "old", "author": "example-owner", "body": "legacy input",
        "created_at": "2026-11-06T01:00:00.000Z"}
    assert len(service.comments(item)["context"]) == 3
    assert service.comments(item)["new"] == []
    result = service.comments(item, cutover_time="2026-11-07T00:00:00.000Z")
    assert [row["comment_id"] for row in result["new"]] == ["human"]
    assert result["new"][0]["origin"] == "human"
    assert result["new"][0]["repo"] == item.issue_repo
    store.acknowledge_comments(item, result["new"])
    assert service.comments(item, cutover_time="2026-11-07T00:00:00.000Z")["new"] == []
    client.comments[item]["human"]["revision"] = "r2"
    assert len(service.comments(item, cutover_time="2026-11-07T00:00:00.000Z")["new"]) == 1
    assert store.get(item)["state"] == "planned"
    assert service.claim_next("c1", "worker") is None


def test_t20_provider_failure_and_foreign_result_stay_local(rig):
    store, _, client, service, _ = rig
    client.list_errors.add("owner/a")
    client.items["owner/b"] = [Snapshot(key(repo="owner/b"), "v1"), Snapshot(key(repo="evil/x"), "v1")]
    result = service.collect("c1")
    assert result["errors"] == {"owner/a": "RuntimeError"}
    assert result["discovered"] == 1
    assert len(store.rows()) == 1
    assert all(entry[2] == "all" for entry in client.reads if entry[0] == "list_items")


def test_t20_targets_and_schemas(repo_root):
    registry = TargetRegistry.load(repo_root / "plugins/subloop/change/targets.yaml")
    assert registry.errors == {}
    assert len(registry.targets) == 5
    server = next(t for t in registry.targets.values() if t.test_recipe == "verification/recipes.yaml#hibi-server")
    assert registry.resolve(key(repo=server.issue_repo)).implementation_repo == server.implementation_repo
    assert server.verify_runner.endswith(":hibi-server-e2e")
    for target_config in registry.targets.values():
        name = target_config.test_recipe.split("#", 1)[1]
        recipes = json.loads((repo_root / "plugins/subloop/change/verification/recipes.yaml").read_text())
        assert recipes[name]["runner"] == target_config.verify_runner
    from axcommon.schemas import validate
    validate("ext/b14/collection/item-key", key().as_dict())
    import yaml
    validate("ext/b14/collection/targets", yaml.safe_load((repo_root / "plugins/subloop/change/targets.yaml").read_text()))


def test_example_target_loads_through_registry(repo_root):
    path = repo_root / "plugins/subloop/change/targets.example.yaml"
    registry = TargetRegistry.load(path)
    assert registry.errors == {}
    target_config = registry.resolve(key(repo="example-org/example-dotnet"))
    assert target_config is not None
    assert target_config.test_recipe == "verification/recipes.yaml#example-dotnet"

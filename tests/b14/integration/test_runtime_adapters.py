"""The real b7 graph and b14 boundaries, with injected transports only."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from axcommon import schemas
from axcommon.defaults import Defaults
from plugins.subloop.change.integration import Journal
from plugins.subloop.change.integration.adapters import CollectionAdapter, VerificationAdapter
from plugins.subloop.change.integration.register import register
from plugins.subloop.change.integration.runtime import build_executor
from plugins.subloop.change.collection import CollectionStore, CollectionService, ItemKey, Snapshot, TargetRegistry
from runtime.subloop.graph import load_yaml
from runtime.subloop.graphcheck import check_graph
from runtime.subloop.nodes import load_registry
from tests.fakes.b14.integration import BASE, HEAD, item, target
from tests.fakes.b7 import make_env


def test_t29_manifest_graph_are_schema_valid_and_graph_checker_accepts(repo_root, harness):
    plugin = repo_root / "plugins/subloop/change"
    manifest = load_yaml(plugin / "manifest.yaml")
    graph = load_yaml(plugin / "graph.yaml")
    schemas.validate("manifest", manifest)
    schemas.validate("graph", graph)
    registry = register(load_registry(repo_root / "plugins/nodes"), harness.flow)
    findings = check_graph(graph, manifest, lib_dir=repo_root / "plugins/subloop/_lib", registry=registry, defaults=Defaults.load())
    assert not [f.as_dict() for f in findings if f.severity == "error"]
    assert any(n.get("include") == "tdd-change" for n in graph["nodes"])
    assert manifest["knobs"]["dry_run"]["default"] is True


def test_t29_runtime_boots_collect_only_and_does_not_listen(tmp_path, repo_root, harness, fake_clock):
    (tmp_path / "b7").mkdir()
    env = make_env(tmp_path / "b7", repo_root, fake_clock)
    ex, flow, errors = build_executor(root=repo_root, services=env.svc, ports=harness.ports,
                                      environ={"AX_DATA_DIR": str(tmp_path / "runtime"), "AX_HOST": "fixture"})
    try:
        assert not errors
        assert ex.mode == "collect_only" and flow is not None
        assert not harness.work.calls and not harness.github.merges
    finally:
        ex.store.close()
        ex.change_journal.close()


def test_t29_runtime_missing_adapters_is_visible_and_local(tmp_path, repo_root, fake_clock):
    (tmp_path / "b7").mkdir()
    env = make_env(tmp_path / "b7", repo_root, fake_clock)
    ex, flow, errors = build_executor(root=repo_root, services=env.svc,
                                      environ={"AX_DATA_DIR": str(tmp_path / "runtime"), "AX_HOST": "fixture"})
    try:
        assert flow is None and any(e["code"] == "change_ports_unconfigured" for e in errors)
        assert ex.mode == "collect_only" and ex.graph.entry == "none"
    finally:
        ex.store.close()
        ex.change_journal.close()


class CollectionClient:
    def __init__(self):
        self.write_calls = []
    def list_items(self, repo, *, state):
        assert state == "all"
        return [Snapshot(ItemKey("github", repo, "pr", 1), HEAD, implementation_pr={"repo": "owner/code", "number": 1})]
    def list_comments(self, key):
        return []
    def observe_work(self, key, external_id):
        from plugins.subloop.change.collection import Observation
        return Observation("lost", external_id)
    def get_comment(self, key, external_id):
        return None
    def get_labels(self, key):
        return set()


def collection_adapter(tmp_path, harness):
    store = CollectionStore(tmp_path / "collection.db", clock=harness.clock)
    config = {**target(), "untrusted_forbidden_paths": [".github/**", "package.json", "Dockerfile*"]}
    registry = TargetRegistry([config])
    client = CollectionClient()
    service = CollectionService(store, registry, client)
    adapter = CollectionAdapter(service, harness.journal,
                                item_builder=lambda row, key, target: {**item(), **key.as_dict()})
    return adapter, store, client


def test_t29_real_collection_adapter_collect_only_has_no_submission(tmp_path, harness):
    adapter, store, client = collection_adapter(tmp_path, harness)
    found = adapter.collect(dry_run=False, mode="collect_only")
    assert len(found) == 1
    assert store.rows()[0]["state"] == "planned" and not client.write_calls
    assert store.pending_operations() == []


def test_t27_real_collection_claim_is_retained_and_terminal_checkpoint_is_idempotent(tmp_path, harness):
    adapter, store, client = collection_adapter(tmp_path, harness)
    found = adapter.collect(dry_run=False, mode="run")
    assert len(found) == 1
    adapter.checkpoint(found[0], "running", "preflight")
    assert store.rows()[0]["state"] == "submitted"
    adapter.checkpoint(found[0], "succeeded", "release_pending")
    adapter.checkpoint(found[0], "succeeded", "release_pending")
    assert store.rows()[0]["state"] == "completed"
    assert not client.write_calls


def test_t27_stale_collection_lease_denies_external_write(tmp_path, harness):
    from plugins.subloop.change.integration import DependencyUnavailable
    adapter, store, client = collection_adapter(tmp_path, harness)
    found = adapter.collect(dry_run=False, mode="run")
    harness.clock.advance(9001)
    with pytest.raises(DependencyUnavailable):
        adapter.checkpoint(found[0], "running", "preflight")
    assert not client.write_calls


def test_t27_verification_adapter_overrides_untrusted_request_fields(harness):
    class Runner:
        def prepare(self, invocation):
            raise ConnectionError("offline")
    bad = {"repo": "attacker/code", "head_sha": "old", "base_sha": "old", "untrusted": False,
           "changed_files": [{"path": "src/a.py", "status": "modified", "binary": False}], "files_complete": True}
    config = {**target(), "test_recipe": "hibi-server"}
    adapter = VerificationAdapter(Runner(), scrub=lambda obj: deepcopy(obj), request_builder=lambda i, t: deepcopy(bad))
    result = adapter.verify(item(trusted=False), config, head_sha=HEAD, base_sha=BASE)
    assert result["status"] == "parked" and result["reason"] == "verify_unavailable"
    assert result["evidence"] == []


def test_t27_real_graph_executes_pr_route_with_expected_head(tmp_path, repo_root, harness, fake_clock):
    (tmp_path / "b7").mkdir()
    env = make_env(tmp_path / "b7", repo_root, fake_clock)
    env.svc.plugin_id = "change"
    env.svc.plugin_dir = repo_root / "plugins/subloop/change"
    harness.collection.items = [item()]
    ex, flow, errors = build_executor(root=repo_root, services=env.svc, ports=harness.ports,
                                      environ={"AX_DATA_DIR": str(tmp_path / "runtime"), "AX_HOST": "fixture",
                                               "AX_CHANGE_LEDGER_PATH": str(tmp_path / "operations.db")})
    assert not errors
    flow.targets = {"owner/issues": target()}
    flow.journal.change_ledger.targets = {"owner/issues"}
    ex.apply("run", {"dry_run": False}, 1)
    async def execute():
        await ex.run_once()
        while ex._tasks:
            pending = [t for t in ex._tasks.values() if not t.done()]
            if not pending:
                break
            await asyncio.gather(*pending)
    try:
        asyncio.run(execute())
        actual = [row for row in ex.store.items() if not row["item_id"].startswith("run:")]
        assert len(actual) == 1 and actual[0]["state"] == "completed", actual
        assert harness.github.merges == [("owner/code", 1, HEAD, "merge")]
        assert len(harness.release.sent) == 1
        assert [op for op, _ in harness.work.calls] == ["prepare_publication"]
    finally:
        ex.store.close()
        ex.change_journal.close()

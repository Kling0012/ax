"""T-20 / T-21 fixtures (network disabled by the common conftest)."""
import pytest
from plugins.subloop.change.collection import CollectionService, CollectionStore, TargetRegistry
from tests.fakes.b14.collection.github import FakeGitHub


def target(repo="owner/a", implementation=None, **overrides):
    return {"issue_repo": repo, "implementation_repo": implementation or repo,
            "merge_method": "merge", "ci_sources": [], "test_recipe": {"verify_runner": "verify-python"},
            "priority_labels": ["urgent", "normal"], "execution_cap": 2,
            "allowed_paths": ["**"], "untrusted_forbidden_paths": [".github/**", "Dockerfile*", "uv.lock"],
            "trusted_authors": ["owner"], "outbound_allowlist": [],
            "state_labels": {"planned": "ax-waiting", "running": "ax-running", "waiting_external": "ax-information",
                             "retry": "ax-retry", "completed": "ax-completed", "parked": "ax-parked"}, **overrides}


@pytest.fixture
def rig(tmp_path):
    class Clock:
        value = 1000.0
        def __call__(self):
            return self.value
    clock = Clock()
    registry = TargetRegistry([target(), target("owner/b"), target("owner/c")])
    store = CollectionStore(tmp_path / "collection.sqlite", clock=clock)
    client = FakeGitHub()
    service = CollectionService(store, registry, client, permission_check=lambda repo, op: True, outbound_check=lambda body: True)
    store.begin_cycle("c1")
    return store, registry, client, service, clock

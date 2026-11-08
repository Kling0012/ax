from copy import deepcopy
from types import SimpleNamespace

import yaml

from core.intake.dest.b12.subloop.bridge import CommonBridge
from core.intake.vias import Ident
from tests.b12.subloop.test_creation import request


class FakeCommon:
    def __init__(self, root):
        self.sha = "a" * 40
        self.git_calls, self.submissions = [], []
        self.rec = {"item": {"kind": "subloop_create", "branch": "subloop/new-one"}, "sha": self.sha}
        self.backend = SimpleNamespace(checkout=root, git=self.git)
        self.deployed = False

    def engine(self, app):
        return self

    def record(self, key):
        return self.rec

    def save(self, rec):
        self.rec = deepcopy(rec)

    def git(self, *args):
        self.git_calls.append(args)
        if args[:2] == ("rev-parse", "HEAD"):
            return self.sha
        if args[0] == "commit":
            self.sha = "registered-sha"
        return ""

    def submit_item(self, app, item, *, approval_id=None, metadata=None):
        self.submissions.append(deepcopy(item))
        return {"todo_id": item["todo_id"]}

    def accept_branch(self, app, key, *, before_accept):
        if not before_accept(self.rec):
            return {"state": "parked", "reason": "manifest_exceeds_declaration"}
        assert self.rec["sha"] == "registered-sha"  # Common verifies this exact commit.
        self.deployed = True
        return {"state": "done", "sha": self.rec["sha"]}


def registration():
    return {"plugin_id": "new-one", "branch": "subloop/new-one", "todo_id": "todo-one",
            "message": "Register new-one\ncreated_by: seat-a", "created_by": "seat-a", "candidate_sha": "a" * 40,
            "quote": request().quote, "tier": "operate", "declaration": request().spec}


def test_common_api_exact_signatures_and_snapshot_commit(world):
    root = world.candidate("new-one")
    common = FakeCommon(root)
    bridge = CommonBridge(object(), validate=world.validate, template_root=world.repo / "templates/subloop", common=common)
    assert bridge.submit_item({"todo_id": "todo-one"})["status"] == 202
    before = (root / "plugins/registry.yaml").read_bytes()
    paths = bridge.plan_registration(root, "new-one", {}, template_root=world.repo)
    assert (root / "plugins/registry.yaml").read_bytes() == before
    result = bridge.accept_branch(registration())
    assert result["accepted"] and common.deployed
    assert common.rec["metadata"]["created_by"] == "seat-a"
    add = next(call for call in common.git_calls if call[0] == "add")
    assert add == ("add", "--", *paths)
    commit = next(call for call in common.git_calls if call[0] == "commit")
    assert registration()["message"] in commit
    assert {entry["id"] for entry in yaml.safe_load((root / "plugins/registry.yaml").read_text())["plugins"]} == {"other", "new-one"}


def test_common_snapshot_manifest_cannot_exceed_earlier_declaration(world):
    root = world.candidate("new-one", secrets=["x-token"])
    before = (root / "plugins/registry.yaml").read_bytes()
    common = FakeCommon(root)
    bridge = CommonBridge(object(), validate=world.validate, template_root=world.repo / "templates/subloop", common=common)
    result = bridge.accept_branch(registration())
    assert result["accepted"] is False and not common.deployed
    assert not any(c[0] in {"commit", "add"} for c in common.git_calls)
    assert (root / "plugins/registry.yaml").read_bytes() == before


def test_common_bridge_service_defers_writes_until_snapshot(world):
    # The initial read-only candidate and common's mutable snapshot are separate.
    initial = world.candidate("new-one")
    world.branches.clear()
    common = FakeCommon(initial)
    bridge = CommonBridge(object(), validate=world.validate, template_root=world.repo / "templates/subloop", common=common)
    world.service.ports.submit_item = bridge.submit_item
    world.service.create("one", request(), Ident("ssh", "commander", "seat-a", "operate"))
    world.branches["subloop/new-one"] = initial
    world.service.ports.accept_branch = bridge.accept_branch
    world.service.stage = bridge.plan_registration
    assert world.service.on_item_state(world.completed("one"))[1]["state"] == "registered"
    assert common.deployed and world.events[-1]["kind"] == "plugin.registered"


def test_staging_same_owned_fragment_is_idempotent(world):
    from core.intake.dest.b12.subloop.registration import read_candidate, stage_registration
    root = world.candidate("new-one")
    manifest, _ = read_candidate(root, "new-one")
    first = stage_registration(root, "new-one", manifest, template_root=world.repo / "templates/subloop")
    before = (root / "plugins/registry.yaml").read_bytes()
    assert stage_registration(root, "new-one", manifest, template_root=world.repo / "templates/subloop") == first
    assert (root / "plugins/registry.yaml").read_bytes() == before

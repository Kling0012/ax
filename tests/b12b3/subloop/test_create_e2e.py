"""Exercise subloop creation through its registered destination handler."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from core.intake.dest.b12b3 import subloop
from core.intake.vias import Ident
from tests.fakes.b12.subloop.world import World


@pytest.fixture
def endpoint(tmp_path, repo_root):
    world = World(tmp_path, repo_root)
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(dest_handlers={})))
    subloop.register(app, world.service)
    yield app, world
    world.store.close()


def payload():
    return {
        "plugin_id": "new-one", "project": "ax", "title": "Create a subloop",
        "body": "Collect public information",
        "spec": {"secrets": [], "outputs": [], "sensitivity": "public"},
        "acceptance": {"examples": ["Collect public data"], "boundaries": ["Do not post"]},
        "quote": "Create a public subloop",
    }


def call(endpoint, data, ident=None, key="request-1"):
    app, _ = endpoint
    ident = ident or Ident("ssh", "commander", "seat-a", "operate")
    handler = app.state.intake.dest_handlers["subloop.create"]
    return handler(app, {"name": "subloop.create"}, {"payload": data, "idempotency_key": key}, ident)


@pytest.mark.parametrize("identity", [
    Ident("ssh", "commander", "seat-a", "read"),
    Ident("worker", "worker", "worker-a", "irreversible"),
    Ident("hq", "hq", "not-user", "irreversible"),
])
def test_rejects_unauthorized_creation(endpoint, identity):
    status, _ = call(endpoint, payload(), identity)
    assert status == 403
    assert not endpoint[1].todos and not endpoint[1].items


@pytest.mark.parametrize("change", ["missing_title", "extra_field", "invalid_spec", "invalid_acceptance"])
def test_rejects_invalid_payload_without_side_effects(endpoint, change):
    data = payload()
    if change == "missing_title":
        del data["title"]
    elif change == "extra_field":
        data["parent_id"] = "unexpected"
    elif change == "invalid_spec":
        data["spec"]["sensitivity"] = "unknown"
    else:
        data["acceptance"]["boundaries"] = []
    status, result = call(endpoint, data)
    assert status == 422 and result["error"]["code"] == "payload_schema"
    assert not endpoint[1].todos and not endpoint[1].items


def test_records_and_submits_once_on_replay(endpoint):
    data = payload()
    status, first = call(endpoint, data)
    assert status == 202 and first["state"] == "queued"
    assert len(endpoint[1].todos) == len(endpoint[1].items) == 1
    assert endpoint[1].todos[0]["request"]["spec"] == data["spec"]
    assert endpoint[1].items[0]["todo_id"] == first["todo_id"]
    assert call(endpoint, deepcopy(data)) == (status, first)
    assert len(endpoint[1].todos) == len(endpoint[1].items) == 1
    changed = deepcopy(data)
    changed["title"] = "Different request"
    assert call(endpoint, changed)[0] == 409
    assert len(endpoint[1].todos) == len(endpoint[1].items) == 1


def test_external_declaration_waits_for_approval(endpoint):
    data = payload()
    data["spec"]["outputs"] = ["github.comment"]
    status, result = call(endpoint, data)
    assert status == 202 and result["reason"] == "approval_required"
    assert not endpoint[1].todos and not endpoint[1].items


@pytest.mark.parametrize("heads,reason", [
    (["a" * 40, "a" * 40], None),
    (["a" * 40, "b" * 40], "candidate_head_changed"),
    (["a" * 40, None], "candidate_head_unavailable"),
    ([None], "candidate_head_unavailable"),
])
def test_candidate_head_is_recorded_and_checked_at_registration(endpoint, heads, reason):
    _, world = endpoint
    assert call(endpoint, payload())[1]["state"] == "queued"
    world.candidate("new-one")
    calls = []

    def read_head(repo, branch):
        calls.append((repo, branch))
        value = heads[len(calls) - 1]
        if value is None:
            raise OSError("head unavailable")
        return value

    world.service.ports.read_head = read_head
    event = world.completed("request-1")
    _, result = world.service.on_item_state(event)
    record = world.store.get("request-1")
    assert all(call == ("work.git", "subloop/new-one") for call in calls)
    if heads[0] is not None:
        assert record["candidate_sha"] == heads[0]
    if reason is None:
        assert result["state"] == "registered"
        assert len(world.accepts) == 1 and world.accepts[0]["candidate_sha"] == heads[0]
    else:
        assert result["state"] == "parked" and record["reason"] == reason
        assert world.parks[-1][1:] == (reason, None)
        world.assert_no_registration()
        assert world.service.on_item_state(event) is None
        assert len(calls) == len(heads)


@pytest.mark.parametrize("artifacts", [
    {"branch": "other/new-one", "remote": "work.git"},
    {"branch": "subloop/new-one", "remote": "other.git"},
])
def test_candidate_outside_allowlist_is_never_recorded(endpoint, artifacts):
    _, world = endpoint
    assert call(endpoint, payload())[1]["state"] == "queued"
    calls = []
    world.service.ports.read_head = lambda *args: calls.append(args)
    _, result = world.service.on_item_state(world.completed("request-1", artifacts=artifacts))
    record = world.store.get("request-1")
    assert result["state"] == "parked" and record["reason"] == "candidate_branch_mismatch"
    assert "candidate_sha" not in record and not calls
    world.assert_no_registration()

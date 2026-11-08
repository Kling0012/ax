"""T-6 / T-123 intake portion: no socket, git, runner or deploy execution."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from axcommon import schemas, timeutil
from axcommon.client import ApiError, Unavailable, WorkerPoolClient
from core.intake.dest.b12 import common, core_change, work
from tests.fakes.b12.work_core.peers import setup, item, completion, approval


def work_payload(**changes):
    payload = {
        "project": "ax",
        "title": "Implement mapper",
        "body": "Map the request to a work item.",
        "acceptance": {"examples": ["maps fields"], "boundaries": ["does not deploy"]},
    }
    payload.update(changes)
    return payload


def core_payload(**changes):
    payload = {"name": "mapper-contract", **work_payload(title="Map core change")}
    payload.update(changes)
    return payload


def test_t123_submit_through_control_and_duplicate():
    app, peers, backend, deployments = setup()
    a = item()
    common.submit_item(app, a)
    assert peers.todos["todo-1"]["state"] == "running"
    assert common.submit_item(app, a)["duplicate"] is True
    assert len([c for c in peers.calls if c[1].endswith("/items")]) == 1
    assert not backend.calls and not deployments
    assert peers.calls[0][1] == "http://ledger:8080/v1/todos"
    assert any(c[1] == "http://control:8080/v1/plugins/work-tdd/items" for c in peers.calls)
    assert all("run_now" not in url and "sl-work-tdd" not in url for _, url, _ in peers.calls)


def test_t6_concurrent_duplicate_only_one_send():
    app, peers, _, _ = setup()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: common.submit_item(app, item()), range(8)))
    assert sum(bool(r.get("duplicate")) for r in results) == 7
    assert len(peers.items) == 1
    assert len([c for c in peers.calls if c[1].endswith("/items")]) == 1


@pytest.mark.parametrize("field", ["examples", "boundaries"])
def test_t123_missing_acceptance_rejected_before_todo(field):
    app, peers, _, _ = setup()
    a = item()
    del a["acceptance"][field]
    with pytest.raises(schemas.ValidationFailed):
        common.submit_item(app, a)
    assert not peers.calls


def test_t6_changed_item_is_not_a_duplicate():
    app, _, _, _ = setup()
    common.submit_item(app, item())
    changed = item()
    changed["body"] = "different task"
    with pytest.raises(ApiError, match="idempotency_mismatch"):
        common.submit_item(app, changed)


def test_t123_inbox_full_preserves_todo_with_one_hour_check(fake_clock):
    app, peers, backend, deployments = setup()
    peers.full = True
    result = common.submit_item(app, item())
    todo = peers.todos["todo-1"]
    assert result["reason"] == todo["reason"] == "inbox_full"
    assert todo["state"] == "parked"
    assert (timeutil.parse_ts(todo["check_at"]) - timeutil.now()).total_seconds() == 3600
    assert not backend.calls and not deployments
    peers.full = False
    todo["state"] = "todo"
    app.state.b12.event({"kind": "task.state", "source": {"component": "ledger"},
                         "data": {"todo_id": "todo-1", "state": "todo"}})
    assert todo["state"] == "running"


def test_t123_resume_keeps_item_and_sends_answer():
    app, peers, _, deployments = setup()
    a = item()
    common.submit_item(app, a)
    peers.todos["todo-1"].update(state="todo", resume_ref={"job_id": "prior-job"}, card={"answer": {"answer": "continue"}, "version": 2})
    app.state.b12.event({"kind": "task.state", "source": {"component": "ledger"},
                         "data": {"todo_id": "todo-1", "state": "todo", "reason": "answered"}})
    assert len(peers.todos) == 1
    assert peers.items["todo-1"]["answer"] == "continue"
    assert peers.items["todo-1"]["resume_ref"] == {"job_id": "prior-job"}
    assert peers.items["todo-1"]["item_key"] == a["item_key"]
    assert peers.todos["todo-1"]["state"] == "running"
    assert not deployments


def test_t123_work_completion_belongs_to_ledger():
    app, peers, backend, deployments = setup()
    common.submit_item(app, item())
    app.state.b12.event(completion())
    assert peers.todos["todo-1"]["state"] == "running"
    assert not backend.calls and not deployments


def test_t123_core_completion_verifies_merges_tags_deploys_once():
    app, peers, backend, deployments = setup()
    approval(app)
    common.submit_item(app, item("core_change"), approval_id="approval-1")
    app.state.b12.event(completion())
    assert backend.calls == ["snapshot", "verify", "merge", "tag"]
    assert peers.todos["todo-1"]["state"] == "done"
    app.state.b12.event(completion(seq=2))
    assert backend.calls == ["snapshot", "verify", "merge", "tag"]
    assert deployments == ["todo-1"]


@pytest.mark.parametrize("stage", ["snapshot", "verify", "merge", "tag"])
def test_t123_failed_acceptance_never_deploys(stage):
    app, peers, backend, deployments = setup()
    approval(app)
    backend.fail = stage
    common.submit_item(app, item("core_change"), approval_id="approval-1")
    app.state.b12.event(completion())
    assert not deployments
    assert peers.todos["todo-1"]["state"] == "parked"
    assert app.state.b12.record("todo-1")["item"]["branch"] == "core/todo-1"
    assert backend.calls[-1] == stage


def test_t123_merge_failure_resumes_at_checkpoint():
    app, peers, backend, deployments = setup()
    approval(app)
    backend.fail = "merge"
    common.submit_item(app, item("core_change"), approval_id="approval-1")
    app.state.b12.event(completion())
    backend.fail = None
    peers.todos["todo-1"]["state"] = "todo"
    app.state.b12.event({"kind": "task.state", "source": {"component": "ledger"},
                         "data": {"todo_id": "todo-1", "state": "todo"}})
    assert backend.calls.count("verify") == 1
    assert backend.calls.count("merge") == 2
    assert deployments == ["todo-1"]


@pytest.mark.parametrize("bad", [None, "unknown"])
def test_t6_no_approval_means_no_todo_or_job(bad):
    app, peers, _, deployments = setup()
    approval(app)
    with pytest.raises(ApiError, match="approval_required"):
        common.submit_item(app, item("core_change"), approval_id=bad)
    assert not peers.calls and not deployments


def test_t6_approval_withdrawn_before_completion():
    app, peers, backend, deployments = setup()
    row = approval(app)
    common.submit_item(app, item("core_change"), approval_id="approval-1")
    row["state"] = "withdrawn"
    app.state.b12.event(completion())
    assert not backend.calls and not deployments
    assert peers.todos["todo-1"]["state"] == "parked"


def test_t6_body_authority_is_not_proof():
    app, peers, _, deployments = setup()
    status, _ = core_change.handle(app, {}, {"payload": {}, "confirm_irreversible": True, "approved_via": "approval-1"}, None)
    assert status == 403
    assert not peers.calls and not deployments


def test_core_executor_uses_only_the_granted_payload():
    app, peers, _, _ = setup()
    row = approval(app, core_payload())
    command = {"destination": "core.change", "issued_by": "user", "approved_via": "approval-1",
               "payload": core_payload(body="forged")}
    with pytest.raises(ApiError, match="approval_payload_mismatch"):
        core_change.execute_approved(app, command)
    command["payload"] = json.loads(row["payload_json"])
    result = core_change.execute_approved(app, command)
    assert peers.items[result["todo_id"]]["kind"] == "core_change"
    assert peers.items[result["todo_id"]]["branch"] == "core/mapper-contract"


def test_t6_approved_core_payload_maps_to_work_item():
    app, peers, _, _ = setup()
    row = approval(app, core_payload())
    result = core_change.execute_approved(app, {"destination": "core.change", "issued_by": "user", "approved_via": "approval-1",
                                                "payload": json.loads(row["payload_json"])})
    assert result["todo_id"].startswith("core-approval-1")
    assert peers.items[result["todo_id"]]["acceptance"] == core_payload()["acceptance"]


@pytest.mark.parametrize("role", sorted(common.READ_ROLES))
def test_t6_read_roles_go_to_pool_with_todo(role):
    app, peers, _, deployments = setup()
    app.state.b12.pool = WorkerPoolClient("http://worker-pool:8081", token="test-intake-token", transport=peers)
    body = {"idempotency_key": "read-1", "payload": {"project": "ax", "title": "Report", "role": role, "body": "Read", "target": "repo", "revision": "a" * 40}}
    status, result = work.handle(app, {}, body, None)
    assert status == 200
    assert peers.todos[result["todo_id"]]["state"] == "running"
    assert peers.jobs[0]["role"] == role
    assert "workdir_id" not in peers.jobs[0]
    assert not peers.items and not deployments
    assert work.handle(app, {}, body, None)[1]["duplicate"] is True
    assert len(peers.jobs) == 1


def test_t6_implementation_request_maps_to_work_item():
    app, peers, _, _ = setup()
    body = {"idempotency_key": "write-1", "payload": work_payload()}
    status, result = work.handle(app, {}, body, None)
    assert status == 200
    assert peers.items[result["todo_id"]]["body"] == work_payload()["body"]
    assert work.handle(app, {}, body, None)[1]["duplicate"] is True


@pytest.mark.parametrize("extra", [{"workdir_id": "repo"}, {"role": "review"}, {"body": "modify repo"}])
def test_t6_direct_job_cannot_smuggle_write_fields(extra):
    app, peers, _, _ = setup()
    status, _ = work.handle(app, {}, {"payload": {"project": "ax", "title": "Read", "role": "research", **extra}}, None)
    assert status == 422
    assert not peers.calls


def test_t6_read_job_delivery_uncertain_is_not_resent():
    app, peers, _, _ = setup()
    app.state.b12.pool = WorkerPoolClient("http://worker-pool:8081", token="test-intake-token", transport=peers)
    body = {"idempotency_key": "read-1", "payload": {"project": "ax", "title": "Report", "role": "research", "body": "Read", "target": "repo", "revision": "a" * 40}}
    original = app.state.b12.pool.submit
    def lose_reply(job):
        original(job)
        raise Unavailable("lost response")
    app.state.b12.pool.submit = lose_reply
    with pytest.raises(Unavailable):
        work.submit_read(app, body)
    with pytest.raises(ApiError, match="job_delivery_uncertain"):
        work.submit_read(app, body)
    assert len(peers.jobs) == 1


def test_t123_durable_cursor_and_untrusted_completions(tmp_path):
    app, peers, backend, deployments = setup(tmp_path / "pending.sqlite3")
    approval(app)
    common.submit_item(app, item("core_change"), approval_id="approval-1")
    forged = completion(seq=1)
    forged["source"]["component"] = "other-subloop"
    mismatch = completion(seq=2)
    mismatch["data"]["todo_id"] = "other-todo"
    peers.pages["item.state"] = [forged, mismatch, completion(seq=3)]
    app.state.b12.poll()
    app.state.b12.poll()
    assert deployments == ["todo-1"]
    assert app.state.b12.db.get("cursor:item.state") == 3
    app.state.b12.db.close()
    app.state.b12.store = common.Store(tmp_path / "pending.sqlite3")
    assert app.state.b12.record("todo-1")["phase"] == "done"
    assert app.state.b12.db.get("cursor:item.state") == 3


def test_t123_subloop_must_pass_pre_registration_check():
    app, peers, backend, deployments = setup()
    common.submit_item(app, item("subloop_create"))
    app.state.b12.finalizers["subloop_create"] = lambda record: False
    app.state.b12.event(completion())
    assert peers.todos["todo-1"]["state"] == "parked"
    assert peers.todos["todo-1"]["reason"] == "manifest_exceeds_declaration"
    assert backend.calls == ["snapshot"]
    assert not deployments


def test_t6_three_registry_destinations_and_sibling_registration(repo_root, monkeypatch):
    import yaml
    import core.intake.dest.b12 as package
    from core.intake.registry import build_registry
    destinations = yaml.safe_load((repo_root / "config/intake-destinations.d/b12.yaml").read_text())
    schemas.validate("intake-destinations", destinations)
    entries, faults = build_registry(destinations)
    assert not faults
    assert set(entries) == {"work.request", "subloop.create", "core.change"}
    assert entries["core.change"]["irreversible"] and entries["core.change"]["tier"] == "irreversible"
    app, _, _, _ = setup()
    called = []
    monkeypatch.setattr(package.importlib, "import_module", lambda name: SimpleNamespace(register=lambda app: called.append(name)))
    package.register(app)
    assert called == ["core.intake.dest.b12.subloop", "core.intake.dest.b12.read_relay"]
    assert set(app.state.intake.dest_handlers) == {"work.request", "core.change", "subloop.create"}

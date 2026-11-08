"""T-217, T-6 and T-123: branch/registry side effects and trust boundaries."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
import yaml

from core.intake.dest.b12 import subloop
from core.intake.dest.b12.subloop import Approval, Request
from core.intake.dest.b12.subloop.policy import Rejected, decide, within_declaration
from core.intake.vias import Ident

OPERATE = Ident("ssh", "commander", "seat-a", "operate")
HQ = Ident("hq", "hq", "user", "irreversible")


def request(**changes):
    return replace(Request("new-one", "ax", "集計を作る", "公開情報を集計する",
                           {"secrets": [], "outputs": [], "sensitivity": "public"},
                           {"examples": ["公開情報だけを読む"], "boundaries": ["外部へ投稿しない"]},
                           "公開情報の集計を作って"), **changes)


def complete(world, key="one", **manifest):
    root = world.candidate("new-one", **manifest)
    return root, world.service.on_item_state(world.completed(key))


def test_t217_a_read_rejected_without_side_effects(world):
    status, body = world.service.create("one", request(), replace(OPERATE, max_tier="read"))
    assert (status, body["reason"]) == (403, "tier_insufficient")
    assert not world.items and not world.todos
    world.assert_no_registration()


@pytest.mark.parametrize("via", ["local", "ssh", "tailnet"])
def test_t217_b_public_assembly_registered_with_attribution(world, via):
    ident = replace(OPERATE, via=via)
    status, body = world.service.create("one", request(), ident)
    assert status == 202 and body["state"] == "queued"
    item = world.items[0]
    assert item["item_key"] == item["todo_id"] == "todo-one"
    assert item["branch"] == "subloop/new-one" and item["kind"] == "subloop_create"
    assert item["role"] == "impl" and item["template"] == "templates/subloop/"
    assert item["acceptance"] == request().acceptance
    assert world.todos[0]["quote"] == request().quote and world.todos[0]["created_by"] == "seat-a"
    world.assert_no_registration()
    root, result = complete(world)
    assert result[1]["state"] == "registered"
    assert world.deploys == [{"op": "deploy"}]
    assert len(world.validations) == 2
    event = next(e for e in world.events if e["kind"] == "plugin.registered")
    assert event["data"] == {"created_by": "seat-a", "quote": request().quote}
    assert "created_by: \"seat-a\"" in world.accepts[0]["message"]
    assert request().quote in world.accepts[0]["message"]
    assert {"plugins/registry.yaml", "compose.d/sl-new-one.yaml"} <= set(world.accepts[0]["paths"])
    registry = yaml.safe_load((root / "plugins/registry.yaml").read_text())
    assert {p["id"] for p in registry["plugins"]} == {"new-one", "other"}
    service = yaml.safe_load((root / "compose.d/sl-new-one.yaml").read_text())["services"]["sl-new-one"]
    assert "ports" not in service and "build" not in service
    assert service["image"] == "ax/runtime:${AX_TAG}"
    assert service["extends"]["service"] == "ax-work-base"
    assert world.cards[-1][1]["waiting_reason"] == "range_review"
    assert world.cards[-1][1]["created_by_label"] == "司令塔 seat-a が作成"
    world.assert_local_only()


@pytest.mark.parametrize("spec", [
    {"secrets": ["x-token"], "outputs": [], "sensitivity": "public"},
    {"secrets": [], "outputs": ["github.comment"], "sensitivity": "public"},
    {"secrets": [], "outputs": [], "sensitivity": "private"},
])
def test_t217_c_operate_external_waits_without_work_or_registry(world, spec):
    status, body = world.service.create("one", request(spec=spec), OPERATE)
    assert (status, body["reason"], body["exit_code"]) == (202, "approval_required", 4)
    assert body["approval_id"] == "pending-one"
    assert not world.items and not world.todos and not world.validations and not world.branches
    world.assert_no_registration()


@pytest.mark.parametrize("key", ["secrets", "outputs", "sensitivity"])
def test_t217_d_all_declarations_required(world, key):
    spec = request().spec.copy()
    spec.pop(key)
    status, body = world.service.create("one", request(spec=spec), OPERATE)
    assert (status, body["reason"]) == (400, "spec_declaration_required")
    world.assert_no_registration()


@pytest.mark.parametrize("quote", [None, "", "abc", "a" * 2001, False, 1234])
def test_t217_d_quote_required(world, quote):
    status, body = world.service.create("one", request(quote=quote), OPERATE)
    assert (status, body["reason"]) == (400, "quote_required")
    assert not world.items
    world.assert_no_registration()


@pytest.mark.parametrize("quote", ["abcd", "a" * 2000])
def test_t217_d_quote_boundaries_accepted(world, quote):
    assert world.service.create("one", request(quote=quote), OPERATE)[1]["state"] == "queued"


@pytest.mark.parametrize("manifest", [
    {"secrets": ["x-token"]}, {"outputs": ["github.comment"]}, {"sensitivity": "private"},
])
def test_t217_e_actual_manifest_exceeds_declaration_parks_and_retains_branch(world, manifest):
    world.service.create("one", request(), OPERATE)
    root = world.candidate("new-one", **manifest)
    original = (root / "plugins/registry.yaml").read_bytes()
    result = world.service.on_item_state(world.completed("one"))
    assert result[1]["state"] == "parked"
    assert world.parks[-1][1] == "manifest_exceeds_declaration"
    assert root.is_dir() and (root / "plugins/registry.yaml").read_bytes() == original
    assert not (root / "compose.d").exists()
    assert world.cards[-1][1]["reason"] == "manifest_exceeds_declaration"
    world.assert_no_registration()


def test_t217_f_approved_hq_reuses_parked_branch(world):
    world.service.create("one", request(), OPERATE)
    root, _ = complete(world, secrets=["x-token"])
    items = len(world.items)
    revised = request(spec={"secrets": ["x-token"], "outputs": [], "sensitivity": "public"})
    world.approvals["approved"] = Approval("approval-1", True)
    status, result = world.service.create("approved", revised, HQ)
    assert status == 202 and result["state"] == "registered"
    assert len(world.items) == items and world.branches["subloop/new-one"] == root
    assert world.events[-1]["data"]["approval_id"] == "approval-1"
    assert world.events[-1]["data"]["created_by"] == "user"
    assert 'approval_id: "approval-1"' in world.accepts[-1]["message"]
    assert "x-token" in (root / "compose.d/sl-new-one.yaml").read_text()
    world.assert_local_only()


def test_t217_f_hq_still_requires_real_approval(world):
    revised = request(spec={"secrets": ["x-token"], "outputs": [], "sensitivity": "public"})
    status, result = world.service.create("one", revised, HQ)
    assert status == 202 and result["reason"] == "approval_required"
    assert not world.items and not world.todos
    world.assert_no_registration()


def test_t217_g_daily_and_total_limits_and_hq_daily_exemption(world):
    world.service = world.make_service(daily_max=1, total_max=3)
    assert world.service.create("one", request(), OPERATE)[0] == 202
    status, body = world.service.create("two", request(plugin_id="second"), OPERATE)
    assert (status, body["reason"]) == (429, "subloop_limit")
    assert world.events[-1]["kind"] == "instruction.rejected"
    assert world.service.create("two", request(plugin_id="second"), HQ)[0] == 202
    world.registered_ids.update({"a", "b"})
    assert world.service.create("three", request(plugin_id="third"), HQ)[0] == 429
    assert len(world.items) == 2
    world.assert_local_only()


def test_t217_g_day_uses_env_timezone_and_is_persistent(world):
    world.service = world.make_service(daily_max=1)
    world.service.create("one", request(), OPERATE)
    world.service = world.make_service(daily_max=1)
    assert world.service.create("two", request(plugin_id="second"), OPERATE)[0] == 429
    world.now += timedelta(minutes=2)  # UTC 15:01 is the next day in Asia/Tokyo.
    assert world.service.create("two", request(plugin_id="second"), OPERATE)[0] == 202


def test_t217_h_register_leaves_stop_handlers_and_conflict_guard_alone(world):
    stop, drain, guard = object(), object(), object()
    state = SimpleNamespace(dest_handlers={"work.stop": stop, "control.command": drain}, conflict_guard=guard)
    app = SimpleNamespace(state=SimpleNamespace(intake=state))
    subloop.register(app)
    assert state.dest_handlers["work.stop"] is stop
    assert state.dest_handlers["control.command"] is drain and state.conflict_guard is guard
    world.assert_local_only()


def test_t217_i_body_claims_never_change_tier_or_approval(world):
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(dest_handlers={}), b12_subloop=world.service))
    app.state.b12_subloop_decode = lambda payload: request(spec=payload["spec"], quote=payload["quote"])
    subloop.register(app)
    payload = {"spec": {"secrets": ["x-token"], "outputs": [], "sensitivity": "public"},
               "quote": request().quote, "tier": "operate", "approved": True, "approval_id": "forged"}
    status, body = app.state.intake.dest_handlers["subloop.create"](
        app, {}, {"idempotency_key": "one", "payload": payload, "approved": True}, OPERATE)
    assert status == 202 and body["reason"] == "approval_required"
    world.assert_no_registration()


@pytest.mark.parametrize("spec", [
    {"secrets": "x", "outputs": [], "sensitivity": "public"},
    {"secrets": [], "outputs": None, "sensitivity": "public"},
    {"secrets": [], "outputs": [], "sensitivity": "other"}, None,
])
def test_t6_malformed_declarations_rejected(world, spec):
    assert world.service.create("one", request(spec=spec), OPERATE)[0] == 400
    world.assert_no_registration()


@pytest.mark.parametrize("ident", [
    Ident("work", "commander", "seat-a", "operate"), Ident("ssh", "manager", "seat-a", "operate"),
    Ident("hq", "hq", "seat-a", "irreversible"), Ident("local", "hq", "user", "irreversible"),
])
def test_t6_internal_or_forged_identity_rejected(world, ident):
    assert world.service.create("one", request(), ident)[0] == 403
    world.assert_no_registration()


def test_t123_submission_idempotency_and_wrong_replay(world):
    world.service.create("one", request(), OPERATE)
    assert world.service.create("one", request(), OPERATE)[1]["state"] == "queued"
    assert len(world.items) == len(world.todos) == 1
    assert world.service.create("one", request(title="別の依頼"), OPERATE)[0] == 409
    assert world.service.create("one", request(), replace(OPERATE, principal="seat-b"))[0] == 409
    world.assert_no_registration()


@pytest.mark.parametrize("acceptance", [{}, {"examples": ["ok"]}, {"boundaries": ["no"]},
                                        {"examples": [], "boundaries": ["no"]}])
def test_t123_acceptance_requires_examples_and_boundaries(world, acceptance):
    assert world.service.create("one", request(acceptance=acceptance), OPERATE)[0] == 400
    assert not world.items


def test_t123_full_inbox_parks_for_one_hour(world):
    world.submit_reason = "inbox_full"
    assert world.service.create("one", request(), OPERATE)[1]["state"] == "parked"
    todo, reason, check_at = world.parks[-1]
    assert todo == "todo-one" and reason == "inbox_full"
    assert check_at == (world.now + timedelta(hours=1)).isoformat()
    world.assert_no_registration()


def test_t123_ejected_item_stays_local(world):
    world.service.create("one", request(), OPERATE)
    result = world.service.on_item_state(world.completed("one", state="ejected"))
    assert result[1]["state"] == "parked" and world.parks[-1][1] == "item_ejected"
    world.assert_no_registration()


def test_t123_completed_replay_and_foreign_events_do_not_register(world):
    world.service.create("one", request(), OPERATE)
    world.candidate("new-one")
    for event in [world.completed("unknown"), {**world.completed("one"), "subloop": "other"},
                  world.completed("one", item_id="another"), world.completed("one", state="running")]:
        assert world.service.on_item_state(event) is None
    world.assert_no_registration()
    assert world.service.on_item_state(world.completed("one"))[1]["state"] == "registered"
    assert world.service.on_item_state(world.completed("one")) is None
    assert len(world.accepts) == len(world.deploys) == 1


def test_validation_failure_does_not_stage_registry(world):
    world.service.create("one", request(), OPERATE)
    root = world.candidate("new-one")
    before = (root / "plugins/registry.yaml").read_bytes()
    world.validate_error = True
    world.service.on_item_state(world.completed("one"))
    assert world.parks[-1][1] == "graph_validation_failed"
    assert (root / "plugins/registry.yaml").read_bytes() == before
    world.assert_no_registration()


def test_actual_manifest_reloaded_after_validation(world):
    world.service.create("one", request(), OPERATE)
    root = world.candidate("new-one")
    def mutate(body):
        if body["kind"] == "graph":
            path = root / "plugins/subloop/new-one/manifest.yaml"
            manifest = yaml.safe_load(path.read_text())
            manifest["outputs"] = ["github.comment"]
            path.write_text(yaml.safe_dump(manifest))
    world.mutate_validation = mutate
    world.service.on_item_state(world.completed("one"))
    assert world.parks[-1][1] == "manifest_exceeds_declaration"
    world.assert_no_registration()


def test_total_limit_rechecked_at_registration(world):
    world.service = world.make_service(total_max=2)
    world.service.create("one", request(), OPERATE)
    world.candidate("new-one")
    world.registered_ids.add("another")
    world.service.on_item_state(world.completed("one"))
    assert world.parks[-1][1] == "subloop_limit" and not world.deploys


def test_acceptance_failure_does_not_deploy(world):
    world.service.create("one", request(), OPERATE)
    world.candidate("new-one")
    world.accept_error = True
    result = world.service.on_item_state(world.completed("one"))
    assert result[1]["state"] == "parked" and not world.deploys
    assert world.parks[-1][1] == "acceptance_failed"
    world.assert_local_only()


def test_missing_wire_contract_is_explicit(world):
    app = SimpleNamespace(state=SimpleNamespace(intake=SimpleNamespace(dest_handlers={})))
    subloop.register(app)
    status, body = app.state.intake.dest_handlers["subloop.create"](app, {}, {}, OPERATE)
    assert status == 503 and body["error"]["code"] == "subloop_unavailable"


def test_declaration_subsets_and_sensitivity_are_ordered():
    spec = {"secrets": ["a", "b"], "outputs": ["github.comment"], "sensitivity": "private"}
    assert within_declaration({"secrets": ["a"], "outputs": [], "sensitivity": "public"}, spec, "irreversible")
    assert not within_declaration({**spec, "sensitivity": "sensitive"}, spec, "irreversible")
    assert not within_declaration(spec, spec, "operate")


@pytest.mark.parametrize("capabilities,valid", [(["run", "draining"], True), (["run"], False)])
def test_t217_h_real_manifest_and_graph_checker_requires_draining(world, capabilities, valid):
    from runtime.subloop.graphcheck import validate_body
    world.service.ports.validate = lambda body: validate_body(body["kind"], body["body"], root=world.repo)
    world.service.create("one", request(), OPERATE)
    world.candidate("new-one", capabilities=capabilities)
    result = world.service.on_item_state(world.completed("one"))
    assert result[1]["state"] == ("registered" if valid else "parked")
    if not valid:
        world.assert_no_registration()


@pytest.mark.parametrize("tier", ["operate", "irreversible"])
@pytest.mark.parametrize("via", ["local", "ssh", "tailnet"])
def test_t188_split_subloop_rows_for_every_commander_receptacle(world, tier, via):
    ident = Ident(via, "commander", "seat-a", tier)
    assert world.service.create("one", request(), ident)[1]["state"] == "queued"
    external = request(plugin_id="external", spec={"secrets": ["x-token"], "outputs": [], "sensitivity": "public"})
    assert world.service.create("external", external, ident)[1]["reason"] == "approval_required"
    assert len(world.items) == 1
    world.assert_no_registration()


def test_untrusted_item_state_source_cannot_trigger_completion(world):
    world.service.create("one", request(), OPERATE)
    world.candidate("new-one")
    assert world.service.on_item_state({**world.completed("one"), "source": {"component": "other"}}) is None
    world.assert_no_registration()


def test_candidate_symlink_cannot_write_or_read_outside_branch(world, tmp_path):
    world.service.create("one", request(), OPERATE)
    root = world.candidate("new-one")
    graph = root / "plugins/subloop/new-one/graph.yaml"
    # A new candidate uses a graph link; the original graph remains untouched.
    (graph.parent / "linked.yaml").symlink_to(graph)
    manifest_file = graph.parent / "manifest.yaml"
    manifest = yaml.safe_load(manifest_file.read_text())
    manifest["graph"] = "linked.yaml"
    manifest_file.write_text(yaml.safe_dump(manifest))
    world.service.on_item_state(world.completed("one"))
    assert world.parks[-1][1] == "candidate_path_invalid"
    world.assert_no_registration()


def test_registered_subloop_is_not_recreated(world):
    world.registered_ids.add("new-one")
    assert world.service.create("one", request(), OPERATE)[0] == 409
    assert not world.items


def test_hq_public_creation_without_quote_is_allowed(world):
    assert world.service.create("one", request(quote=None), HQ)[1]["state"] == "queued"


def test_pending_limit_persists_after_reopening_database(world):
    from core.intake.dest.b12.subloop import Store
    world.service = world.make_service(daily_max=1)
    world.service.create("one", request(), OPERATE)
    world.store.close()
    world.store = Store(world.tmp / "pending.sqlite3")
    world.service = world.make_service(daily_max=1)
    assert world.service.create("two", request(plugin_id="second"), OPERATE)[0] == 429
    assert world.service.create("one", request(), OPERATE)[1]["state"] == "queued"
    assert len(world.items) == 1


def test_range_review_delivery_failure_replays_without_redeploy(world):
    world.service.create("one", request(), OPERATE)
    world.candidate("new-one")
    calls = []
    original = world.card
    def card(todo_id, body):
        calls.append(body)
        if len(calls) == 1:
            raise RuntimeError("ledger unavailable")
        original(todo_id, body)
    world.service.ports.card = card
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        world.service.on_item_state(world.completed("one"))
    assert world.store.get("one")["phase"] == "registered"
    assert world.service.on_item_state(world.completed("one")) is None
    assert world.store.get("one")["range_card_written"]
    assert len(world.deploys) == len(world.accepts) == 1
    assert len([e for e in world.events if e["kind"] == "plugin.registered"]) == 1

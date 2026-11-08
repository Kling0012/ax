"""Register S-19 stages in the shared b7 executor without editing its library."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from runtime.subloop.nodeapi import eject, ok, skip
from runtime.subloop.nodes import NodeType

from .flow import ChangeFlow, item_key
from .gate import Decision, independent_safe, requirements_complete, valid_verification
from .ports import DependencyUnavailable

STAGES = ("collect", "prepare", "route", "answer", "investigate", "verify", "publish", "merge", "record")


def register(registry, flow: ChangeFlow | None):
    for stage in STAGES:
        name = "change." + stage
        if name in registry.types:
            raise ValueError("duplicate integration node")
        meta = {"statuses": ["ok", "skip", "error"], "outcomes": ["question", "bug", "pr", "change", "repair"],
                "output": {"answer": "github.comment", "publish": "github.pr_update", "merge": "github.merge"}.get(stage),
                "publishes": stage in {"answer", "publish"}, "launching": stage in {"answer", "publish", "merge"},
                "llm": False, "role_field": None, "writes_repo": False}

        async def run(ctx, inp, _stage=stage):
            if flow is None:
                return eject("change_ports_unconfigured")
            try:
                if _stage in {"answer", "publish", "merge"} and ctx.mode == "run" and ctx.knobs.get("dry_run", True) is False:
                    await ctx.launching(f"change:{ctx.item_id}:{_stage}", {"item_id": ctx.item_id, "stage": _stage})
                result = await ctx.call(_step, flow, _stage, ctx)
            except Exception:
                # Persist an item-local hold even when an adapter fails after a claim.
                # Exception text may contain credentials or personal data.
                if _stage != "collect":
                    try:
                        saved = flow.journal.get("pipeline", ctx.item_id) or {}
                        item = saved.get("item") or ctx.item
                        return await ctx.call(_held, flow, item, "integration_unavailable")
                    except Exception:
                        pass
                return eject("integration_unavailable")
            sender = getattr(ctx.svc.sink, "send", None)
            if callable(sender):
                await ctx.call(flow.journal.flush, sender)
            return result

        async def reconcile(ctx, key, _stage=stage):
            if flow is None:
                return None
            # All writes have their own durable receipts. Invoke only the
            # reconciliation-aware stage, never a raw external write.
            result = await run(ctx, {"with": {}}, _stage=_stage)
            return {"found": True, "out": result}

        registry.types[name] = NodeType(name, str(Path(__file__)), {"type": "object", "additionalProperties": False}, run,
                                        reconcile if meta["launching"] else None, meta)
    if flow is not None and registry.get("worker.run") is not None:
        native = registry.get("worker.run")
        original = native.run
        async def budgeted_worker(ctx, inp):
            saved = flow.journal.get("pipeline", ctx.item_id) or {}
            source = saved.get("item") or {}
            if source.get("kind") == "issue" and source.get("pr") is None:
                target = flow.targets.get(source.get("issue_repo", "").lower())
                prior = saved.get("snapshot") or {}
                try:
                    current = await ctx.call(flow.ports.github.view, source, target)
                    fresh = (current.get("snapshot_type") == "issue" and
                             current.get("head_sha") == prior.get("head_sha") and
                             current.get("input_revision") == prior.get("input_revision"))
                except Exception:
                    fresh = False
                if not fresh:
                    return await ctx.call(_held, flow, source, "reviewed_input_changed", "retry_wait", flow.clock() + 60)
            if flow.model_calls + 1 > ctx.knobs.get("change_model_calls_per_cycle", 30):
                return eject("model_budget_exhausted")
            flow.model_calls += 1
            return await original(ctx, inp)
        native.run = budgeted_worker
    return registry

def register_graph_types(registry):
    """Register the static change stages for graph validation without a live flow."""
    return register(registry, None)


def _held(flow, item, reason, route="parked", next_check=None):
    result = flow.route(item, Decision(False, reason, route, next_check))
    return eject(reason, artifacts={"next_check": next_check}, data=result)


def _step(flow, stage, ctx):
    dry = ctx.knobs.get("dry_run", True) is not False
    if stage == "collect":
        flow.model_calls = 0
        flow.model_calls_per_cycle = ctx.knobs.get("change_model_calls_per_cycle", 30)
        flow.fix_rounds = ctx.knobs.get("change_fix_rounds", 3)
        started = flow.clock()
        flow.journal.event("change.cycle", ctx.run_id, {"phase": "started", "targets": len(flow.targets)})
        try:
            items = flow.ports.collection.collect(dry_run=dry, mode=ctx.mode)
            cap = ctx.knobs.get("change_starts_per_cycle", 4)
            selected = items[:cap] if ctx.mode == "run" and not dry else items
            selected = [{**item, "item_id": item.get("item_id") or item_key(item)} for item in selected]
            flow.journal.event("change.cycle", ctx.run_id,
                               {"phase": "completed", "targets": len(flow.targets), "found": len(items),
                                "started": 0, "held": len(items) - len(selected), "model_calls": 0,
                                "elapsed_s": flow.clock() - started, "dry_run": dry})
            return ok({"items": selected})
        except Exception:
            flow.journal.event("change.cycle", ctx.run_id, {"phase": "failed", "elapsed_s": flow.clock() - started})
            raise
    saved = flow.journal.get("pipeline", ctx.item_id) or {}
    item = deepcopy(saved.get("item") or ctx.item)
    target = flow.targets.get(item["issue_repo"].lower())
    if not target or item.get("implementation_repo", "").lower() != target["implementation_repo"].lower():
        return _held(flow, item, "target_unavailable")
    if dry or ctx.mode != "run":
        return skip(flow.process(item, dry_run=dry, mode=ctx.mode))
    if (flow.journal.get("stopped", item_key(item)) or {}).get("stopped"):
        return _held(flow, item, "item_stopped")
    if stage == "prepare":
        service = getattr(flow.ports.collection, "service", None)
        if item["issue_repo"].lower() in getattr(service, "failed_repos", set()):
            return _held(flow, item, "collection_incomplete")
        snapshot = flow.ports.github.view(item, target)
        safety = flow._safety(item, snapshot)
        if not independent_safe(safety, head=snapshot.get("head_sha"), revision=snapshot.get("input_revision")):
            reason = safety.get("reason") if isinstance(safety, dict) else None
            if reason == "safety_pending" and safety.get("status") == "pending":
                return _held(flow, item, reason, "retry_wait", flow.clock() + 60)
            known = {"safety_unavailable", "safety_submission_unknown", "safety_lookup_unknown",
                     "safety_job_failed", "safety_evidence_invalid", "safety_not_independent",
                     "safety_rejected", "safety_tool_isolation_unverified", "summary_injection"}
            return _held(flow, item, reason if reason in known else "safety_failed")
        evidence_store = getattr(flow.ports.github, "evidence_store", None)
        if evidence_store is not None:
            evidence_store.record_safety(item, snapshot, safety)
        summaries = (safety.get("worker_input") or {}).get("summaries")
        if not isinstance(summaries, list) or len(summaries) != 2 or any(not isinstance(s, str) or not s for s in summaries):
            return _held(flow, item, "neutral_summary_unavailable")
        repair = flow.journal.get("repair", item_key(item)) or {}
        if repair.get("fix_rounds", 0):
            item["route"] = "change"
        previous = saved.get("snapshot") or {}
        if saved.get("verification_pending") and (previous.get("head_sha") != snapshot.get("head_sha") or
                                                   previous.get("input_revision") != snapshot.get("input_revision")):
            saved["verification_pending"] = False
        saved.update(item=item, safety=safety, snapshot=snapshot, fix_rounds=repair.get("fix_rounds", 0))
        flow.ports.collection.checkpoint(item, "running", "stage_boundary")
        # The item visible to tdd-change contains inspected summaries only.
        clean = {k: item[k] for k in ("item_id", "project", "provider", "issue_repo", "kind", "number", "implementation_repo", "pr") if k in item}
        clean.update(title="Change request", body="\n".join(summaries))
        if item.get("route") in {"change", "bug"}:
            plan = flow.ports.work.plan(clean, target, safety=safety)
            if not isinstance(plan, dict) or any(not plan.get(k) for k in ("base_ref", "branch", "verify")):
                return _held(flow, item, "tdd_configuration_unavailable")
            clean.update({k: plan.get(k, "") for k in ("base_ref", "branch", "verify", "answer", "resume_ref")})
        else:
            clean.update(base_ref=snapshot.get("base_sha", ""), branch="", verify="", answer="", resume_ref="")
        ctx.store.update_item(ctx.item_id, data=clean)
        flow.journal.put("pipeline", ctx.item_id, saved)
        return ok({"publication_text": json.dumps(safety["worker_input"], ensure_ascii=False),
                   **{k: clean[k] for k in ("base_ref", "branch", "verify", "answer", "resume_ref")}})
    if stage == "route":
        # A pending host job resumes verification; it must not restart TDD work.
        route = "pr" if saved.get("verification_pending") is True else item.get("route", "pr")
        if route not in {"question", "bug", "pr", "change"}:
            return _held(flow, item, "route_unavailable")
        return ok({"outcome": route})
    if stage == "answer":
        result = flow._question(item, target)
        return ok(result) if result["status"] == "succeeded" else eject(result["reason"], data=result)
    if stage == "investigate":
        result = flow._work(item, target, "investigate", safety=saved["safety"])
        if result.get("cause_known") is not True:
            return _held(flow, item, "diagnostic_required")
        return ok({"outcome": "change"})
    if stage == "verify":
        outputs = ctx.outputs()
        worktree = (outputs.get("tdd-change.worktree") or {}).get("data", {})
        if worktree.get("workdir_id"):
            item["workdir_id"] = worktree["workdir_id"]
            item["branch"] = worktree.get("branch", "")
            saved["item"] = item
        snapshot = flow.ports.github.view(item, target)
        safety = flow._safety(item, snapshot)
        if not independent_safe(safety, head=snapshot.get("head_sha"), revision=snapshot.get("input_revision")):
            reason = safety.get("reason") if isinstance(safety, dict) else None
            if reason == "safety_pending" and safety.get("status") == "pending":
                return _held(flow, item, reason, "retry_wait", flow.clock() + 60)
            known = {"safety_unavailable", "safety_submission_unknown", "safety_lookup_unknown",
                     "safety_job_failed", "safety_evidence_invalid", "safety_not_independent",
                     "safety_rejected", "safety_tool_isolation_unverified", "summary_injection"}
            return _held(flow, item, reason if reason in known else "safety_failed")
        verified = flow.ports.verification.verify(item, target, head_sha=snapshot["head_sha"], base_sha=snapshot["base_sha"])
        saved.update(snapshot=snapshot, safety=safety, verification=verified,
                     verification_pending=(verified.get("status") == "parked" and verified.get("reason") == "verification_pending"))
        flow.journal.put("pipeline", ctx.item_id, saved)
        if verified.get("status") == "parked":
            reason = verified.get("reason", "verify_unavailable")
            if reason == "verification_pending":
                return _held(flow, item, reason, "retry_wait", flow.clock() + 60)
            return _held(flow, item, reason)
        valid = valid_verification(verified, repo=item["implementation_repo"], base=snapshot["base_sha"], head=snapshot["head_sha"],
                                  untrusted=item.get("author") not in target["trusted_authors"])
        if valid:
            evidence_store = getattr(flow.ports.github, "evidence_store", None)
            if evidence_store is not None:
                evidence_store.record_safety(item, snapshot, safety)
                evidence_store.record_verification(item, snapshot, verified)
        has_pr = type(item.get("pr")) is int and item["pr"] > 0
        if not valid or not requirements_complete(snapshot.get("requirements"), snapshot.get("required_ids")) or (has_pr and snapshot.get("mergeable") is not True):
            return _repair(flow, ctx, saved, target)
        return ok({"verified_head": snapshot["head_sha"], "unperformed": verified.get("unperformed", [])})
    if stage == "publish":
        snapshot = saved["snapshot"]
        result = flow.publish(item, target, saved["safety"], snapshot)
        if result.get("status") != "delivered":
            route = "parked" if result.get("status") == "blocked" else "retry_wait"
            return _held(flow, item, result.get("reason", "publication_pending"), route, result.get("next_retry"))
        saved["item"] = flow.adopt_publication(item, result)
        flow.journal.put("pipeline", ctx.item_id, saved)
        return ok({"published_head": snapshot["head_sha"]})
    if stage == "merge":
        result = flow.merge(item, target, saved["safety"], saved["verification"])
        if result["status"] in {"fix", "resolve_conflict"}:
            return _repair(flow, ctx, saved, target)
        return ok(result) if result["status"] == "succeeded" else eject(result["reason"], data=result)
    if stage == "record":
        flow.journal.event("change.route", ctx.item_id, {"target": item_key(item), "reason": "complete", "result": "succeeded", "next_check": None})
        return ok({"recorded": True})
    raise DependencyUnavailable("unknown_stage")


def _repair(flow, ctx, saved, target):
    item = saved["item"]
    rounds = (flow.journal.get("repair", item_key(item)) or {}).get("fix_rounds", saved.get("fix_rounds", 0))
    if rounds >= ctx.knobs.get("change_fix_rounds", flow.fix_rounds):
        trusted = item.get("author") in target["trusted_authors"]
        return _held(flow, item, "trusted_internal_decision_required" if trusted else "untrusted_fix_limit")
    saved["fix_rounds"] = rounds + 1
    plan = flow.ports.work.plan(item, target, safety=saved["safety"])
    if any(not plan.get(k) for k in ("base_ref", "branch", "verify")):
        return _held(flow, item, "tdd_configuration_unavailable")
    current = ctx.store.get_item(ctx.item_id)["data"]
    current.update({k: plan.get(k, "") for k in ("base_ref", "branch", "verify", "answer", "resume_ref")})
    ctx.store.update_item(ctx.item_id, data=current)
    flow.journal.put("pipeline", ctx.item_id, saved)
    flow.journal.put("repair", item_key(item), {"fix_rounds": rounds + 1})
    ev = flow.journal.event("change.repair", ctx.item_id, {"round": rounds + 1, "head_sha": saved["snapshot"]["head_sha"]})
    flow.learning.record({"id": f"change:{ev}", "source_event": ev, "project": item["project"], "reason": "repair",
                          "evidence_ref": ev, "status": "pending"})
    return _held(flow, item, "repair_required", "retry_wait", flow.clock() + 60)

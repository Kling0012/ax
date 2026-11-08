"""Read-only handoff assembled from the authenticated commander's read scope."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import shlex

import yaml

from axcommon import ids
from axcommon.defaults import Defaults, config_dir
from axcommon.redact import from_environment

NAMES = ("summary", "waiting_user", "approvals", "running", "claims", "events", "commitments", "decisions", "next")
SAFE_FIELDS = frozenset({"id", "todo_id", "job_id", "command_id", "approval_id", "kind", "state", "status", "project", "subject", "seq", "ts", "created_at", "updated_at", "due_at", "check_at", "expires_at", "active", "target", "target_key", "mode", "redacted", "type", "own", "lease_until", "acquired_at", "owner", "principal", "session", "via", "agent", "model", "metadata_status"})


def _time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt.astimezone(timezone.utc)


def _free_text_allowed(model):
    if not model:
        return False
    try:
        root = config_dir()
        commander = yaml.safe_load((root / "commander-models.yaml").read_text())
        registry = yaml.safe_load((root / "models.yaml").read_text())
        rows = commander["models"]
        registered = next(row for row in registry["models"] if row.get("id") == model and row.get("status", "active") == "active")
        return (registered.get("tier") in {"standard", "premium", "frontier"}
                and any(model.startswith(row.get("match", "\0")) and row.get("max_class") == "sensitive" for row in rows))
    except (OSError, KeyError, TypeError, ValueError, StopIteration, yaml.YAMLError):
        return False


def _safe(value, free, redactor):
    if isinstance(value, list):
        return [_safe(v, free, redactor) for v in value]
    if isinstance(value, dict):
        value = {k: _safe(v, free, redactor) for k, v in value.items() if free or k in SAFE_FIELDS}
        return redactor.redact_obj(value)[0]
    return redactor.redact_obj(value)[0] if isinstance(value, str) else value


async def _events(relay, params, since):
    result, after = [], 0
    for _ in range(100):
        body = await relay._get("timeline", "/v1/events", {**params, "after": after, "limit": 1000})
        rows = body["events"]
        if not isinstance(rows, list):
            raise ValueError("bad events")
        result.extend(row for row in rows if _time(row["ts"]) >= since and
                      (row.get("kind") == "task.state" or row.get("kind") == "health.alert" or
                       row.get("kind") == "claim.preempted" or str(row.get("kind", "")).startswith(("control.", "approval."))))
        if len(rows) < 1000:
            return sorted(result, key=lambda r: (_time(r["ts"]), r.get("seq", 0)), reverse=True)
        next_after = body["next_after"]
        if not isinstance(next_after, int) or next_after <= after:
            raise ValueError("bad cursor")
        after = next_after
    raise ValueError("events pagination limit")


def approval_rows(app, project, now):
    rows = app.state.approvals.store.list()
    output = []
    for row in rows:
        if row.get("state") != "pending_user" or (project and row.get("project") != project):
            continue
        if _time(row["expires_at"]) <= now:
            continue
        output.append({key: row.get(key) for key in ("approval_id", "destination", "requested_by", "project", "state", "created_at", "expires_at")})
    return output


def claim_rows(app, ident, now):
    handler = app.state.intake.dest_handlers["claim.op"]
    with handler._lock:
        if handler.file.broken:
            raise ValueError("claim store unavailable")
        rows = list(handler.file.items())
        output = []
        for target, record in rows:
            if _time(record["lease_until"]) <= now or _time(record["max_until"]) <= now:
                continue
            owner = record["owner"]
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            output.append({"target_key": target, "owner": dict(owner), "own": owner.get("principal") == ident.principal,
                           "lease_until": record["lease_until"], "acquired_at": record["acquired_at"],
                           "agent": metadata.get("agent"), "model": metadata.get("model"),
                           "project": metadata.get("project"),
                           "metadata_status": "ok" if all(metadata.get(k) for k in ("agent", "model", "project")) else "unknown"})
    return sorted(output, key=lambda row: row["acquired_at"])


def work_projects(app):
    engine = app.state.b12
    with engine.lock:
        rows = engine.db.work_requests()
    projects = {}
    for row in rows:
        job = row.get("job_id")
        payload = row.get("payload")
        if not isinstance(job, str) or not isinstance(payload, dict) or not isinstance(payload.get("project"), str):
            continue
        project = payload["project"]
        if job in projects and projects[job] != project:
            projects[job] = None
        elif job not in projects:
            projects[job] = project
    return projects


async def brief(app, relay, ident, query, declared=None):
    defaults = Defaults.load()
    limit = int(defaults.get("brief_max_items"))
    if query.get("short") == "1":
        limit = min(limit, 5)
    now = datetime.now(timezone.utc)
    since = query.get("since") or (now - timedelta(hours=defaults.get("brief_since_h"))).isoformat()
    try:
        since_time = _time(since)
        section = query.get("section")
        offset = int(query.get("offset", "0"))
        if offset < 0 or (offset and section is None):
            raise ValueError()
        if section is not None and section not in NAMES and section not in tuple(map(str, range(9))):
            raise ValueError()
    except (TypeError, ValueError):
        return 400, {"error": {"code": "bad_request"}}
    project = query.get("project")
    params = {"project": project} if project else {}
    model = (declared or {}).get("model", "")
    free = _free_text_allowed(model)
    redactor = from_environment()
    redactor.allow_email_domains = frozenset()
    if not redactor.index_ok or not redactor.pii_list_ok:
        free = False

    async def fetch(component, path, key, params=None):
        body = await relay._get(component, path, params)
        data = body[key]
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise ValueError("bad response")
        return data

    async def capture(coro):
        try:
            return await coro
        except Exception:
            return None

    todos, commands, plugins, jobs, events, promises, goals, decisions, approvals, claims, job_projects = await asyncio.gather(*map(capture, [
        fetch("ledger", "/v1/todos", "todos", params),
        fetch("control", "/v1/commands", "commands"),
        fetch("control", "/v1/plugins", "plugins"),
        fetch("timeline", "/v1/projections/jobs", "rows", {"active": "true"}),
        _events(relay, params, since_time),
        fetch("ledger", "/v1/promises", "promises", {"state": "open"}),
        fetch("ledger", "/v1/goals", "goals", {"state": "active"}),
        fetch("ledger", "/v1/decisions", "decisions", {**params, "status": "active"}),
        asyncio.to_thread(approval_rows, app, project, now),
        asyncio.to_thread(claim_rows, app, ident, now),
        asyncio.to_thread(work_projects, app),
    ]))
    goals_partial = jobs_partial = claims_partial = False
    if project:
        # Goals have no project column; promises are related through their ToDo.
        if todos is None:
            promises = None
        elif promises is not None:
            todo_ids = {t.get("id") for t in todos}
            promises = [p for p in promises if p.get("todo_id") in todo_ids]
        if goals is not None and todos is not None:
            goal_ids = {goal_id for todo in todos for goal_id in (todo.get("goal_ids") or [])}
            goals_partial = any(goal.get("id") not in goal_ids for goal in goals)
            goals = [goal for goal in goals if goal.get("id") in goal_ids]
        else:
            goals = None
        if jobs is not None and job_projects is not None:
            jobs_partial = any(not job_projects.get(row.get("key")) for row in jobs)
            jobs = [row for row in jobs if job_projects.get(row.get("key")) == project]
        else:
            jobs = None
        # Control commands have no verified project. Old claims may lack one.
        commands = None
        if claims is not None:
            claims_partial = any(not claim.get("project") for claim in claims)
            claims = [claim for claim in claims if claim.get("project") == project]

    sections = [{"name": name, "status": "unavailable", "items": []} for name in NAMES]
    def put(index, items, available, next_cmd):
        if items is None:
            return
        clean = _safe(items, free, redactor)
        start = offset if section is not None else 0
        omitted = max(0, len(clean) - start - limit)
        command = None
        if omitted:
            prefix = "ax --model " + shlex.quote(model) if free and model else "ax"
            command = prefix + " brief --section " + NAMES[index] + " --offset " + str(start + limit) + " --since " + shlex.quote(since)
            if project:
                command += " --project " + shlex.quote(project)
            if query.get("short") == "1":
                command += " --short"
        sections[index].update(status="ok" if available else "unavailable", items=clean[start:start + limit],
                               omitted=omitted, next=command)
    waiting = None if todos is None else sorted((t for t in todos if t.get("state") == "waiting_user"), key=lambda t: t.get("updated_at") or t.get("created_at") or "")
    approval_cards = []
    if waiting is not None:
        for todo in waiting:
            card = todo.get("card")
            if isinstance(card, dict) and card.get("approval_id"):
                view = card.get("approval") if isinstance(card.get("approval"), dict) else {}
                if isinstance(view.get("data"), dict):
                    view = view["data"]
                approval_cards.append({"approval_id": card["approval_id"],
                                       **{k: view[k] for k in ("destination", "requested_by", "expires_at", "state") if k in view}})
    running = []
    if todos is not None:
        running.extend({"type": "todo", **t} for t in todos if t.get("state") in {"running", "review"})
    if jobs is not None:
        running.extend({"id": r.get("key"), "type": "job", **r.get("value", {})} for r in jobs if isinstance(r.get("value"), dict))
    if commands is not None:
        running.extend({"type": "command", **c} for c in commands if c.get("state") in {"active", "expired_pending"})
    put(1, waiting, waiting is not None, "ax todos --state waiting_user")
    put(2, approvals if approvals is not None else approval_cards, approvals is not None, "")
    put(3, running, all(x is not None for x in (todos, jobs, commands)) and not jobs_partial, "ax status")
    put(4, claims, claims is not None and not claims_partial, "")
    put(5, events, events is not None, "ax events")
    items6 = ([{"type": "promise", **p} for p in promises] if promises is not None else []) + ([{"type": "goal", **g} for g in goals] if goals is not None else [])
    put(6, items6, promises is not None and goals is not None and not goals_partial, "ax todos")
    if decisions is not None:
        decisions = sorted((d for d in decisions if d.get("kind") in {"decision", "preference", "handoff"}), key=lambda d: d.get("ts") or "", reverse=True)
    put(7, decisions, decisions is not None, "ax decisions")
    actions = []
    if waiting:
        actions.extend({"kind": "waiting_user", "id": t.get("id")} for t in waiting)
    actions.extend({"kind": "approval", "id": a.get("approval_id")} for a in (approvals if approvals is not None else approval_cards))
    if promises is not None:
        for p in promises:
            try:
                if _time(p["due_at"]) < now:
                    actions.append({"kind": "overdue_promise", "id": p.get("id")})
            except (KeyError, TypeError, ValueError):
                continue
    if todos is not None:
        for t in todos:
            try:
                if t.get("state") == "parked" and _time(t["check_at"]) <= now:
                    actions.append({"kind": "parked", "id": t.get("id")})
            except (KeyError, TypeError, ValueError):
                continue
    if commands is not None:
        actions.extend({"kind": "expired_pending", "id": c.get("id")} for c in commands if c.get("state") == "expired_pending")
    if claims is not None:
        old_cutoff = now - timedelta(minutes=defaults.get("claim_lease_min"))
        actions.extend({"kind": "old_claim", "target_key": c["target_key"]} for c in claims if _time(c["acquired_at"]) <= old_cutoff)
    complete_actions = all(x is not None for x in (todos, approvals, promises, commands, claims)) and not claims_partial
    put(8, actions, complete_actions, "")
    guide_version = None
    try:
        from tools.gen_commander_guide import load_bundle
        guide_version = load_bundle().guide_version
    except Exception:
        pass
    overall = None
    if plugins is not None:
        modes = [p.get("mode") for p in plugins]
        overall = ("stopped" if "draining" in modes else
                   "warning" if any(m in {"collect_only", "poll_paused"} for m in modes) else
                   "normal" if all(m == "run" for m in modes) else None)
    if overall != "stopped" and (waiting or approvals or any(t.get("state") == "parked" for t in (todos or []))):
        overall = "warning"
    if goals_partial:
        sections[6]["partial_reason"] = "goal_project_unknown"
    if jobs_partial:
        sections[3]["partial_reason"] = "job_project_unknown"
    if claims_partial:
        sections[4]["partial_reason"] = "claim_project_unknown"
    if project:
        sections[3].setdefault("partial_reason", "command_project_unknown")
    identity = {"seat_id": ident.principal, "tier": ident.max_tier, "via": ident.via}
    identity_ok = True
    if ident.via in {"ssh", "tailnet"}:
        try:
            seat = app.state.seats.store.get(ident.principal)
            identity.update({key: seat[key] for key in ("device", "device_id", "expires_at") if key in seat})
        except Exception:
            identity_ok = False
    sections[0].update(identity=identity,
                       guide_version=guide_version, retrieved_at=now.isoformat(),
                       waiting_user_count=len(waiting) if waiting is not None else None,
                       approval_count=len(approvals) if approvals is not None else None,
                       stopped_count=(sum(t.get("state") == "parked" for t in todos) if todos is not None else None),
                       overall=overall, status="ok" if all(x is not None for x in (plugins, todos, approvals)) and guide_version and identity_ok else "unavailable")
    if section is not None:
        sections = [sections[int(section) if section.isdigit() else NAMES.index(section)]]
    elif query.get("short") == "1":
        sections = sections[:4]
    result = {"since": since, "redacted": not free, "sections": sections}
    if query.get("new_session") == "1":
        result["session"] = ids.new_ulid()
    return 200, _safe(result, True, redactor)

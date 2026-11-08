from __future__ import annotations

import pytest

from axcommon import schemas


def acceptance():
    return {"examples": ["works"], "boundaries": ["does not publish"]}


VALID = {
    "work.request": {"project": "ax", "title": "Map work", "body": "Implement it.", "acceptance": acceptance()},
    "core.change": {"name": "mapper-contract", "project": "ax", "title": "Map core", "body": "Implement it.", "acceptance": acceptance()},
    "subloop.create": {
        "plugin_id": "new-loop", "project": "ax", "title": "Create loop", "body": "Assemble it.", "acceptance": acceptance(),
        "spec": {"secrets": [], "outputs": [], "sensitivity": "public"}, "quote": "利用者の依頼",
    },
    "permission.change": {"project": "ax", "repo": "owner/repo", "destination": "release", "operations": ["deploy"], "source_instruction": "instruction-1"},
    "work.stop": {"todo_id": "todo-1"},
    "work.cancel": {"job_id": "job-1", "reason": "superseded"},
    "work.resume": {"todo_id": "todo-1"},
    "schedule.update": {"op": "create", "id": "schedule-1", "body": "Review", "start_at": "2026-11-07T00:00:00Z", "end_at": "2026-11-07T01:00:00Z", "state": "planned"},
    "life_state.update": {"kind": "focus", "state": "focus", "since": "2026-11-07T00:00:00Z", "as_of": "2026-11-07T00:00:00Z"},
    "goal.update": {"op": "create", "id": "goal-1", "body": "Ship AX", "state": "active"},
    "commitment.update": {"op": "create", "id": "commitment-1", "body": "Review AX", "state": "open", "todo_id": "todo-1", "due_at": "2026-11-08T00:00:00Z"},
}


@pytest.mark.parametrize(("name", "payload"), VALID.items())
def test_schema_mappers_accept_documented_payloads(name, payload):
    assert schemas.errors(f"dest/{name}.json", payload) == []


@pytest.mark.parametrize(("name", "payload"), VALID.items())
def test_schema_mappers_reject_unknown_fields(name, payload):
    assert schemas.errors(f"dest/{name}.json", {**payload, "unknown": True})

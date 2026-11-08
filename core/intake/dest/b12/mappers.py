"""S-6.4 destination payloads normalized to the S-7.10 work-item contract."""
from __future__ import annotations


def work_item(payload: dict, todo_id: str) -> dict:
    """Map an implementation request after its destination schema has passed."""
    item = {
        "item_key": todo_id,
        "todo_id": todo_id,
        "kind": "work",
        "project": payload["project"],
        "role": payload.get("role", "impl"),
        "title": payload["title"],
        "body": payload["body"],
        "acceptance": payload["acceptance"],
    }
    if "verify" in payload:
        item["verify"] = payload["verify"]
    return item


def core_change_item(payload: dict, todo_id: str) -> dict:
    """Map an approved core-change payload to its dedicated work branch."""
    item = {
        "item_key": todo_id,
        "todo_id": todo_id,
        "kind": "core_change",
        "project": payload["project"],
        "role": "impl",
        "title": payload["title"],
        "body": payload["body"],
        "acceptance": payload["acceptance"],
        "branch": "core/" + payload["name"],
    }
    if "verify" in payload:
        item["verify"] = payload["verify"]
    return item

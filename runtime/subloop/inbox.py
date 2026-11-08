"""POST /items の受け取り（S-7.10）。HTTP と分けてあるので、starlette なしで試験できる。"""
from __future__ import annotations

from .executor import Executor


def accept_item(ex: Executor, body: dict) -> tuple[int, dict]:
    """POST /items の本体（HTTP と分けて試験する）。戻り: (状態コード, 本文)。"""
    acc = body.get("acceptance") if isinstance(body, dict) else None
    if not isinstance(acc, dict) or not acc.get("examples") or not acc.get("boundaries"):
        return 400, {"error": {"code": "acceptance_required", "message": "acceptance.examples と acceptance.boundaries の両方が要る（受け入れの例と境界の例）"}}
    from axcommon import schemas

    errs = schemas.errors("work-item", body)
    if errs:
        return 422, {"error": {"code": "bad_request", "message": "; ".join(errs[:5])}}
    key = body["item_key"]
    inbox_max = int(ex.knobs().get("inbox_max", 50))
    r = ex.store.inbox_put(key, body, inbox_max=inbox_max)
    if r == "duplicate":
        return 200, {"accepted": True, "duplicate": True, "item_key": key, "state": ex.store.inbox_state(key)}
    if r == "full":
        return 429, {"error": {"code": "inbox_full", "message": f"受け取り待ちが上限 {inbox_max} に達している"}}
    ex.svc.emit("item.state", key, {"item_id": key, "state": "planned", "reason": None, "todo_id": body.get("todo_id")}, subloop=ex.svc.plugin_id)
    ex.kick()
    return 200, {"accepted": True, "duplicate": False, "item_key": key, "state": "queued"}

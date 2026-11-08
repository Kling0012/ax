"""通知からの操作（S-22.4、R-H21、R-H22、T-54）。

流れ: 通知の参照（`#/todo/<ID>`）→ HQ が同じ ToDo を開く（open_target）→ カードから完了（complete）か保留（park）を指示する。
  - 認証は HQ の入口（S-22.7）が済ませた利用者の操作だけがここに来る（HTTP の口は ext/b10a.py。認証できない要求は拒否する）。
  - 操作は入口 `from=hq` の行き先 `ledger.complete` / `ledger.park`（S-6.4）へ出す。HQ は台帳を直接書かない。
  - 検証: 先に台帳の遷移表（S-9.2）で許される遷移かを確かめ、不正なら入口へ出さず 409 invalid_transition で拒否する。
    保留は reason と check_at が必須（S-9.2）。`user_only` のカードの完了と保留は HQ からだけ（ここは HQ なので通る）。
  - 成功は、入口の応答が成功で、かつ台帳を読み直して状態が変わっていたときだけ（「台帳の更新後に成功」）。
  - 冪等: 同じ op_id の再送は、最初の結果をそのまま返す（入口へ出し直さない）。op_id が違っても、対象が既に目的の状態なら
    `already` の成功として返す（二重に動かさない）。目的と違う終端の状態（done や dropped）への操作は拒否する。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from axcommon import timeutil
from axcommon.client import ApiError, Unavailable

from .intake import IntakeClient, IntakeError
from .question import LedgerPort
from .store import NotifyStore, serialized

log = logging.getLogger("hq.notify.actions")

# 遷移表（S-9.2）のうち、通知から出せる 2 つの操作が許される元の状態
COMPLETE_FROM = {"running": "done", "review": "done", "waiting_user": "done"}
PARK_FROM = {"todo": "parked", "running": "parked", "review": "parked"}
TARGET_STATE = {"complete": "done", "park": "parked"}
DESTINATION = {"complete": "ledger.complete", "park": "ledger.park"}
ALLOWED_FROM = {"complete": COMPLETE_FROM, "park": PARK_FROM}


def _res(ok: bool, status: int, **kw: Any) -> dict:
    return {"ok": ok, "status": status, **kw}


class NotifyActions:
    def __init__(self, ledger: LedgerPort, intake: IntakeClient, store: NotifyStore, *, now: Callable[[], Any] | None = None):
        self.ledger, self.intake, self.store = ledger, intake, store
        self._now = now or timeutil.now

    # --- 対象を開く ---
    def open_target(self, todo_id: str) -> dict:
        try:
            todo = self.ledger.get(todo_id)
        except (Unavailable, ApiError):
            return _res(False, 503, error="ledger_unavailable")
        if todo is None:
            return _res(False, 404, error="not_found")
        card = todo.get("card") if isinstance(todo.get("card"), dict) else None
        return _res(True, 200, todo={"id": todo["id"], "title": todo.get("title"), "state": todo.get("state"),
                                     "reason": todo.get("reason"), "check_at": todo.get("check_at"), "owner": todo.get("owner")},
                    card=({"question": card.get("question"), "options": card.get("options", []), "answered": card.get("answer") is not None,
                           "version": card.get("version", 1)} if card else None),
                    actions=self._available(todo.get("state")))

    @staticmethod
    def _available(state: str | None) -> list[str]:
        return [a for a, table in ALLOWED_FROM.items() if state in table]

    # --- 操作 ---
    @serialized
    def apply(self, *, todo_id: str, action: str, op_id: str, reason: str | None = None, check_at: str | None = None) -> dict:
        if action not in TARGET_STATE:
            return _res(False, 422, error="unknown_action")
        if not isinstance(op_id, str) or not op_id or len(op_id) > 120:
            return _res(False, 422, error="op_id_required")
        prev = self.store.action_get(op_id)
        if prev is not None:
            if prev["todo_id"] != todo_id or prev["action"] != action:
                return _res(False, 409, error="op_id_reused")
            return {**prev["result"], "replayed": True}
        result = self._apply_once(todo_id, action, reason, check_at, op_id)
        # 終端の結果（成功と、入口・台帳が理由つきで拒否した結果）は残す。つなげない・台帳が未更新（503、502）は残さず、同じ op_id で再送できる。
        if result["status"] not in (502, 503):
            self.store.action_put(op_id, todo_id, action, result["ok"], result["status"], result, timeutil.format_ts(self._now()))
        return {**result, "replayed": False}

    def _apply_once(self, todo_id: str, action: str, reason: str | None, check_at: str | None, op_id: str) -> dict:
        try:
            todo = self.ledger.get(todo_id)
        except (Unavailable, ApiError):
            return _res(False, 503, error="ledger_unavailable")
        if todo is None:
            return _res(False, 404, error="not_found")
        state = todo.get("state")
        target = TARGET_STATE[action]
        if state == target:
            return _res(True, 200, already=True, state=state)   # 既に目的の状態（冪等）
        if state not in ALLOWED_FROM[action]:
            return _res(False, 409, error="invalid_transition", state=state, target=target)
        payload: dict = {"todo_id": todo_id}
        if action == "park":
            if not reason or not check_at:
                return _res(False, 422, error="park_requires_reason_and_check_at")
            try:
                timeutil.parse_ts(check_at)
            except ValueError:
                return _res(False, 422, error="check_at_invalid")
            payload.update({"reason": reason, "check_at": check_at})
        elif reason:
            payload["reason"] = reason
        try:
            status, body = self.intake.submit(DESTINATION[action], payload, idempotency_key=f"notify:{op_id}")
        except IntakeError:
            return _res(False, 503, error="intake_unavailable")
        if status >= 300 or body.get("ok") is False:
            err = ((body.get("error") or {}) if isinstance(body.get("error"), dict) else {})
            return _res(False, status if status >= 400 else 409, error=err.get("code") or body.get("reason") or "rejected")
        # 台帳の更新後に成功とする
        try:
            after = self.ledger.get(todo_id)
        except (Unavailable, ApiError):
            return _res(False, 503, error="ledger_unverified")
        if after is None or after.get("state") != target:
            return _res(False, 502, error="ledger_not_updated", state=(after or {}).get("state"))
        return _res(True, 200, state=after["state"])

"""未回答の質問の通知（S-22.3、R-H16、R-H17、T-53）。

対象: 台帳の `waiting_user` のカード（質問、選択肢、回答は同じカードにある。別カードは作らない）。
規則:
  - 通知の重複は (ToDo ID, 質問の版) をキーに抑える。最初の「成功」通知から `question_renotify_h` 時間が経つまでは再通知しない。
    失敗は成功として数えない（失敗した通知は dispatcher が再配送する。ここは成功の時刻だけで判断する）。
  - 質問の版（card.version）が変わったら別の質問として扱う（すぐ通知する）。
  - 回答済み・閉じた（waiting_user でなくなった）カードには再通知しない。送る前の保持分は cancelled にする。
  - 理由のない質問（waiting_reason が無い）は通知しない。そもそも作らせないのは台帳（S-9.2）で、ここは二重の防御。
  - 再通知の時間は制御 API の knob で変えられる（既定は S-17 `question_renotify_h` = 48）。set_renotify_h() で値を渡す。
"""
from __future__ import annotations

import logging
import math
import os
from datetime import timedelta
from typing import Any, Callable

from axcommon import timeutil
from axcommon.client import ApiError, LedgerClient, Unavailable

from . import KIND_QUESTION
from .dispatcher import Dispatcher, Notification, open_ref
from .store import NotifyStore

log = logging.getLogger("hq.notify.question")

OPEN_STATES = ("pending", "held", "failed")


class LedgerPort:
    """台帳の読み取り（質問の走査と、操作の確かめ）。差し替えられる。"""

    def __init__(self, client: LedgerClient):
        self.client = client

    def waiting(self) -> list[dict]:
        resp = self.client.list_todos(state="waiting_user")
        rows = resp.get("todos") if isinstance(resp, dict) else resp
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise Unavailable("ledger_invalid_response")
        return rows

    def get(self, todo_id: str) -> dict | None:
        try:
            return self.client.get_todo(todo_id)
        except ApiError as e:
            if e.status == 404:
                return None
            raise


def default_renotify_h(defaults=None) -> float:
    env = os.environ.get("AX_QUESTION_RENOTIFY_H")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            pass
    if defaults is not None:
        try:
            return float(defaults.get("question_renotify_h"))
        except Exception:  # noqa: BLE001
            pass
    return 48.0


class QuestionNotifier:
    def __init__(self, ledger: LedgerPort, store: NotifyStore, dispatcher: Dispatcher, *, renotify_h: float = 48.0,
                 now: Callable[[], Any] | None = None):
        self.ledger, self.store, self.dispatcher = ledger, store, dispatcher
        self._renotify_h = float(renotify_h)
        self._now = now or timeutil.now
        self.skipped_no_reason = 0

    @property
    def renotify_h(self) -> float:
        return self._renotify_h

    def set_renotify_h(self, hours: float) -> bool:
        """knob の変更。正の数だけ受ける。"""
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours <= 0:
            return False
        self._renotify_h = float(hours)
        return True

    @staticmethod
    def _version(card: dict) -> int:
        v = card.get("version", 1)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else 1

    def tick(self) -> dict:
        out = {"notified": 0, "suppressed": 0, "cancelled": 0, "skipped_no_reason": 0, "error": None}
        try:
            waiting = self.ledger.waiting()
        except (Unavailable, ApiError) as e:
            out["error"] = type(e).__name__   # 台帳が見えない間は何もしない（他の処理は続ける）
            return out
        now = self._now()
        valid = [t for t in waiting if isinstance(t.get("card"), dict)
                 and isinstance(t.get("id"), str) and t["card"].get("question")
                 and t["card"].get("waiting_reason") in
                 {"physical", "secret_input", "money", "publish", "irreversible", "preference", "range_review"}
                 and t["card"].get("answer") is None]
        live = {(t["id"], self._version(t["card"])) for t in valid}
        # Cancel before submit can drain older held rows.
        out["cancelled"] = self._cancel_closed(live)
        for todo in waiting:
            card = todo.get("card")
            tid = todo.get("id")
            if not isinstance(tid, str) or not isinstance(card, dict) or not card.get("question") or card.get("waiting_reason") not in {"physical", "secret_input", "money", "publish", "irreversible", "preference", "range_review"}:
                self.skipped_no_reason += 1
                out["skipped_no_reason"] += 1
                continue
            if card.get("answer") is not None:
                continue   # 回答済み。再通知しない
            qv = self._version(card)
            live.add((tid, qv))
            last = self.store.question_last_success(tid, qv)
            if last is not None and now < timeutil.parse_ts(last) + timedelta(hours=self._renotify_h):
                out["suppressed"] += 1
                continue
            if self._recent_attempt(tid, qv, now):
                out["suppressed"] += 1   # 失敗・検査止めの直後に毎周期つくり直さない（窓が過ぎたら新しく試す）
                continue
            res = self.dispatcher.submit(Notification(
                kind=KIND_QUESTION, title=f"回答待ち: {todo.get('title', '')}", message=str(card["question"]),
                todo_id=tid, qversion=qv, ref=open_ref(tid), priority=4, tags=("question",),
                dedupe_key=f"question:{tid}:{qv}"))
            if res["state"] != "duplicate":
                out["notified"] += 1
            else:
                out["suppressed"] += 1
        return out

    def _recent_attempt(self, tid: str, qv: int, now) -> bool:
        since = timeutil.format_ts(now - timedelta(hours=self._renotify_h))
        for row in self.store.list(todo_id=tid, kind=KIND_QUESTION):
            if row["qversion"] == qv and row["state"] in ("pending", "held", "failed", "blocked") and row["created_at"] >= since:
                return True
        return False

    def _cancel_closed(self, live: set[tuple[str, int]]) -> int:
        n = 0
        for state in OPEN_STATES:
            for row in self.store.list(kind=KIND_QUESTION, state=state):
                if (row["todo_id"], row["qversion"]) not in live:
                    self.store.update(row["id"], state="cancelled", last_error="closed_before_delivery")
                    n += 1
        return n

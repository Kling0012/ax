"""台帳の常時監視（D-8、S-9.3）。双方向の購読と5分ごとの走査。

- 時系列記録の `item.state`（data.todo_id を持つ物）を `GET /v1/events?kind=item.state&wait=` で購読し、
  `completed`（kind: work の ToDo のみ）で running → review、`ejected` で running → parked（check_at は今＋1日）
  へ遷移させる（S-9.3、D-8）。サブループは ToDo を遷移させない（作業側の口は transition を通さない）。
- check_at を過ぎた parked に `task.state`（data.due=true）を5分周期で書く（D-8。管理役がそれを受ける）。
- 読む位置（after）は台帳の保存先 `timeline_cursor` に持ち、再起動後は続きから読む（D-8）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from axcommon.client import ApiError, TimelineClient, Unavailable
from axcommon.defaults import Defaults

from .service import LedgerService

log = logging.getLogger("core.ledger.watch")

ITEM_STATE_KIND = "item.state"
CURSOR_KIND = "item.state"
WAIT_S = 30  # longpoll の既定（S-17 longpoll_s）
POLL_INTERVAL_S = 300  # parked の走査は5分ごと（D-8）


def extract_after(resp: Any) -> tuple[list[dict], str | None]:
    """GET /v1/events の応答の形を2つ許す（S-3.5 の応答の形は B1 が確かめる。OPEN.md b0）。"""
    if isinstance(resp, list):
        events = resp
        return events, (str(events[-1].get("seq")) if events and events[-1].get("seq") is not None else None)
    if isinstance(resp, dict):
        events = resp.get("events") or resp.get("items") or []
        nxt = resp.get("next_after") or resp.get("after")
        if nxt is None and events and events[-1].get("seq") is not None:
            nxt = str(events[-1]["seq"])
        return events, (str(nxt) if nxt is not None else None)
    return [], None


def order_of(ev: dict) -> str:
    """再送に耐える順序の鍵。seq が無い応答でも位置を進められるようにする。"""
    if ev.get("seq") is not None:
        return str(ev["seq"])
    return str(ev.get("id") or "")


class LedgerWatcher:
    def __init__(self, service: LedgerService, timeline: TimelineClient | None, *,
                 defaults: Defaults | None = None, sleep: Callable[[float], None] = time.sleep):
        self.service = service
        self.timeline = timeline
        self.defaults = defaults or Defaults.load()
        self._sleep = sleep
        self._stop = False
        self.failures = 0
        self.poll_count = 0

    def stop(self) -> None:
        self._stop = True

    def step(self) -> dict:
        """購読を最大1回試みる。戻り値は {handled: [...], after: ...}。"""
        after = self.service.store.get_cursor(CURSOR_KIND)
        params = {"kind": ITEM_STATE_KIND, "wait": WAIT_S}
        if after is not None:
            params["after"] = after
        try:
            resp = self.timeline.get_events(**params) if self.timeline else None
            self.failures = 0
        except (Unavailable, ApiError) as e:
            self.failures += 1
            raise
        events, next_after = extract_after(resp)
        handled = self.service.handle_item_events(events)
        if next_after is not None:
            self.service.store.set_cursor(CURSOR_KIND, next_after)
        elif events:
            self.service.store.set_cursor(CURSOR_KIND, order_of(events[-1]))
        self.poll_count += 1
        if handled:
            log.info("item.state の反映: %s", handled)
        return {"handled": handled, "events": len(events), "after": self.service.store.get_cursor(CURSOR_KIND)}

    def sweep(self) -> int:
        return self.service.sweep_parked()

    def run(self, *, max_rounds: int | None = None) -> int:
        """購読（longpoll の待ちの分だけ進む）と、5分ごとの走査を回す。試験では max_rounds を渡す。"""
        from axcommon import timeutil

        rounds = 0
        last_sweep = timeutil.now()
        while not self._stop:
            rounds += 1
            try:
                self.step()
            except (Unavailable, ApiError):
                log.exception("時系列記録の購読に失敗（次に再試行する）")
                self._sleep(POLL_INTERVAL_S)
            if (timeutil.now() - last_sweep).total_seconds() >= POLL_INTERVAL_S:
                self.sweep()
                last_sweep = timeutil.now()
            if max_rounds is not None and rounds >= max_rounds:
                break
            self._sleep(WAIT_S)
        return rounds


def make_watcher(service: LedgerService, timeline: TimelineClient | None, **kw) -> LedgerWatcher:
    return LedgerWatcher(service, timeline, **kw)

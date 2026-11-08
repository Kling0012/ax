"""朝の要約（S-22.5、R-H18、T-53 の周辺）。

朝の定時（日本時間。既定は S-17 `morning_digest_time_jst` = 07:00 JST。制御 API の knob で変えられる）に、前日の活動と未完了の約束を
集めて利用者へ通知する。規則:
  - 取得できなかった情報は「欠落」として本文に書く。取得失敗を空の結果（0 件）と扱わない。
  - 同じ対象日（前日）の要約は 1 回だけ作る。通知が失敗して残っているときは、配信状態を照合してから再送する（二重に作らない）。
  - 事象 `digest.generated`（対象日、件数、欠落の名前、通知 ID）を書く。
本文は短い要約（件数と先頭の数件の題）。秘密・個人情報の検査は dispatcher の guard が行う。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from axcommon import timeutil
from axcommon.client import ApiError, LedgerClient, Unavailable

from . import KIND_DIGEST
from .dispatcher import Dispatcher, Notification
from .store import NotifyStore, serialized

log = logging.getLogger("hq.notify.digest")

JST = timezone(timedelta(hours=9))
TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?:\s*(?:JST|Asia/Tokyo))?\s*$")
ITEMS_MAX = 5


def parse_time_jst(text: str) -> tuple[int, int] | None:
    m = TIME_RE.match(text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


class DigestSources(Protocol):
    def done_on(self, date: str) -> list[dict]: ...
    def open_promises(self) -> list[dict]: ...


class LedgerDigestSources:
    """台帳から集める。取れなければ例外を出す（呼ぶ側が欠落として扱う）。"""

    def __init__(self, client: LedgerClient):
        self.client = client

    def done_on(self, date: str) -> list[dict]:
        resp = self.client.list_todos(state="done")
        todos = self._items(resp, "todos")
        out = []
        for t in todos:
            ts = t.get("updated_at")
            if isinstance(ts, str):
                try:
                    if timeutil.parse_ts(ts).astimezone(JST).date().isoformat() == date:
                        out.append(t)
                except ValueError:
                    continue
        return out

    def open_promises(self) -> list[dict]:
        resp = self.client.get("/v1/promises", params={"state": "open"})
        return self._items(resp, "promises")

    @staticmethod
    def _items(resp, key: str) -> list[dict]:
        rows = resp.get(key) if isinstance(resp, dict) else resp
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid_ledger_response")
        return rows


class MorningDigest:
    def __init__(self, sources: DigestSources, store: NotifyStore, dispatcher: Dispatcher, *, time_jst: str = "07:00 JST",
                 now: Callable[[], Any] | None = None, sink=None):
        self.sources, self.store, self.dispatcher, self.sink = sources, store, dispatcher, sink
        self._now = now or timeutil.now
        self._hm = parse_time_jst(time_jst) or (7, 0)

    def set_time(self, text: str) -> bool:
        hm = parse_time_jst(text)
        if hm is None:
            return False
        self._hm = hm
        return True

    @property
    def time_text(self) -> str:
        return f"{self._hm[0]:02d}:{self._hm[1]:02d} JST"

    def target_date(self, now) -> str:
        return (now.astimezone(JST) - timedelta(days=1)).date().isoformat()

    def is_due(self, now) -> bool:
        local = now.astimezone(JST)
        return (local.hour, local.minute) >= self._hm

    # --- 本文 ---
    def build(self, date: str) -> tuple[str, str, list[str], dict]:
        """(件名, 本文, 欠落の名前, 件数)。"""
        missing: list[str] = []
        counts: dict[str, int] = {}
        lines: list[str] = []
        details: list[str] = []
        try:
            done = self.sources.done_on(date)
            counts["done"] = len(done)
            lines.append(f"{date} の完了: {len(done)}件")
            # Inspect full source text before the dispatcher shortens the summary.
            details.extend(str(t.get("title", "")) for t in done[:ITEMS_MAX])
        except (Unavailable, ApiError, ValueError, KeyError, TypeError):
            missing.append("done")
            lines.append(f"{date} の完了: 取得できなかった")
        try:
            promises = self.sources.open_promises()
            counts["open_promises"] = len(promises)
            lines.append(f"未完了の約束: {len(promises)}件")
            for p in promises[:ITEMS_MAX]:
                due = p.get("due_at") or p.get("due") or "期限なし"
                details.append(f"・{str(p.get('text') or p.get('title') or p.get('body') or '')}（{str(due)}）")
        except (Unavailable, ApiError, ValueError, KeyError, TypeError):
            missing.append("open_promises")
            lines.append("未完了の約束: 取得できなかった")
        return f"朝の要約 {date}", "\n".join(lines + details), missing, counts

    # --- 周期 ---
    @serialized
    def run_if_due(self) -> dict:
        now = self._now()
        date = self.target_date(now)
        if not self.is_due(now):
            return {"state": "not_due", "date": date}
        row = self.store.digest_get(date)
        if row is not None:
            return self._reconcile(date, row)
        # Recover if notification persistence succeeded before digest indexing.
        previous = self.store.by_dedupe(f"digest:{date}")
        if previous is not None:
            self.store.digest_put(date, previous["id"], previous["state"], timeutil.format_ts(now))
            return self._reconcile(date, self.store.digest_get(date))
        title, message, missing, counts = self.build(date)
        res = self.dispatcher.submit(Notification(kind=KIND_DIGEST, title=title, message=message, priority=3, tags=("sunrise",),
                                                  dedupe_key=f"digest:{date}"))
        state = res["state"]
        self.store.digest_put(date, res["id"], state, timeutil.format_ts(now))
        if self.sink is not None:
            try:
                self.sink.emit("digest.generated", date, {"date": date, "counts": counts, "missing": missing,
                                                         "notification_id": res["id"], "result": res["state"]})
            except Exception:  # noqa: BLE001
                log.error("digest.generated を書けない")
        return {"state": state, "date": date, "missing": missing, "notification_id": res["id"]}

    def _reconcile(self, date: str, row: dict) -> dict:
        """作成済みの要約。通知の配信状態を照合し、送れていれば sent にする（再作成はしない。再配送は dispatcher）。"""
        if row["state"] == "sent":
            return {"state": "sent", "date": date}
        n = self.store.get(row["notification_id"]) if row["notification_id"] else None
        if n is not None:
            if n["state"] != row["state"]:
                self.store.digest_put(date, n["id"], n["state"], timeutil.format_ts(self._now()))
            return {"state": n["state"], "date": date}
        return {"state": row["state"], "date": date}

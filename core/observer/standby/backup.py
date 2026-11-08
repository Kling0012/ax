"""予備役の本体（S-11.3、S-11.4、D-10.2）。

オブザーバーが `Observer.set_hook("backup", standby.on_alert)` 経由で呼ぶ。オブザーバーは先に `backup.activated` を書いてある。

on_alert(alert)  周期を止めないため、別スレッドの待ち行列に積んで返る（同時に走るのは 1 件。続きの警報は順に処理する）。
activate(alert)  同期の本体。順番:
  1 活動の記録（自分の保存先。頻度の数え直しに使う）
  2 予備役の周回を 1 回（CycleRunner）。失敗（NotImplementedError、例外、ok でない）しても次へ進む
  3 `manager.cycle`（役割 backup_manager）を 1 件書く
  4 頻度を確認し、根本原因の ToDo を必ず 1 件作る（上限以上なら最優先。台帳が落ちていれば溜める）
  5 上限以上なら利用者向けのカードを足す（窓の中で 1 回だけ）
drain_check(ctx)  オブザーバーの検査として登録する。周期ごとに溜めた ToDo を作り直す。警報は返さない。
"""
from __future__ import annotations

import collections
import json
import logging
import os
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from axcommon import timeutil
from axcommon.client import LedgerClient, TimelineClient
from axcommon.events import EventSink

from . import ISSUED_BY, ROLE
from .cycle import CycleRequest, CycleResult, CycleRunner
from .frequency import ActivationLog, count_activations
from .rootcause import TodoSender, frequent_card, root_cause_todo

log = logging.getLogger("observer.standby")

QUEUE_MAX = 20


class Standby:
    def __init__(self, *, runner: CycleRunner | None, ledger: LedgerClient | None, timeline: TimelineClient | None,
                 sink: EventSink | None, data_dir: str | os.PathLike, window_days: float = 7, limit: int = 2,
                 inline: bool = False, now: Callable[[], Any] | None = None):
        self.runner, self.timeline, self.sink = runner, timeline, sink
        self.dir = Path(data_dir)
        self.window_days, self.limit = float(window_days), int(limit)
        self.inline = inline
        self._now = now or timeutil.now
        self.log = ActivationLog(self.dir)
        self.todos = TodoSender(ledger, self.dir / "outbox")
        self._state_path = self.dir / "state.json"
        self._q: collections.deque = collections.deque()
        self._cv = threading.Condition()
        self._busy = False
        self._worker: threading.Thread | None = None
        self.activations = 0
        self.cycle_failures = 0
        self.dropped = 0
        self.last_error: str | None = None

    # --- 状態（頻発の印を窓の中で 1 回だけ出すため） ---
    def _state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, st: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, self._state_path)

    # --- オブザーバーから ---
    def on_alert(self, alert) -> None:
        if self.inline:
            self.activate(alert)
            return
        with self._cv:
            if len(self._q) >= QUEUE_MAX:
                self.dropped += 1
                return
            self._q.append(alert)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="standby", daemon=True)
                self._worker.start()
            self._cv.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cv:
                if not self._q:
                    self._worker = None
                    self._busy = False
                    self._cv.notify_all()
                    return
                alert = self._q.popleft()
                self._busy = True
            try:
                self.activate(alert)
            except Exception as e:  # noqa: BLE001  予備役の失敗でオブザーバーを止めない
                self.last_error = type(e).__name__
                log.error("予備役の処理の失敗: %s", type(e).__name__)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """試験用。待ち行列が空で処理中でなくなるまで待つ。"""
        import time

        end = time.monotonic() + timeout
        with self._cv:
            while self._q or self._busy:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.05))
        return True

    # --- 本体 ---
    def activate(self, alert) -> dict:
        now = self._now()
        ts = timeutil.format_ts(now)
        self.activations += 1
        self.log.add(ts, alert.check, alert.target)
        trigger = {"check": alert.check, "target": alert.target, "detail": alert.detail}

        result, note = self._run_cycle(trigger)
        self._emit_cycle(alert, result, note)

        count = count_activations(timeline=self.timeline, log=self.log, now=now, days=self.window_days)
        root = root_cause_todo(check=alert.check, target=alert.target, activation_event=getattr(alert, "activation_event", None), top=count >= self.limit)
        root_state = self.todos.send(root)

        out: dict[str, Any] = {"cycle": note, "root_cause": root_state, "root_cause_id": root["id"], "frequent": False,
                               "count": count}
        if count >= self.limit:
            out["frequent"] = self._escalate(now, count, alert)
        return out

    def _run_cycle(self, trigger: dict) -> tuple[CycleResult | None, str]:
        if self.runner is None:
            self.cycle_failures += 1
            return None, "runner_unavailable"
        try:
            res = self.runner.run(CycleRequest(trigger=trigger))
        except NotImplementedError:
            self.cycle_failures += 1
            return None, "not_implemented"
        except Exception as e:  # noqa: BLE001
            self.cycle_failures += 1
            self.last_error = type(e).__name__
            return None, f"error:{type(e).__name__}"
        if not res.ok:
            self.cycle_failures += 1
            return res, f"not_ok:{res.reason or 'unknown'}"
        return res, "ok"

    def _emit_cycle(self, alert, res: CycleResult | None, note: str) -> None:
        if self.sink is None:
            return
        data = {"role": ROLE, "issued_by": ISSUED_BY, "trigger": f"{alert.check}:{alert.target}", "ok": note == "ok",
                "result": note, "actions": res.actions if res else 0}
        if res and res.model:
            data["model"] = res.model
        if res and res.cycle_id:
            data["cycle_id"] = res.cycle_id
        try:
            self.sink.emit("manager.cycle", f"{ROLE}:{alert.check}:{alert.target}", data)
        except Exception:  # noqa: BLE001
            log.error("manager.cycle を書けない")

    def _escalate(self, now, count: int, alert) -> bool:
        """上限以上: 利用者向けのカードを、窓の中で 1 回だけ足す（S-11.4、R-O4）。"""
        st = self._state()
        last = st.get("escalated_at")
        if isinstance(last, str):
            try:
                if timeutil.parse_ts(last) > now - timedelta(days=self.window_days):
                    return False
            except ValueError:
                pass
        card = self.todos.send(frequent_card(count=count, window_days=self.window_days, limit=self.limit))
        st["escalated_at"] = timeutil.format_ts(now)
        st["escalated_count"] = count
        self._save_state(st)
        return card != "rejected"

    # --- オブザーバーの検査として登録（周期ごとに溜めた ToDo を作り直す） ---
    def drain_check(self, ctx) -> list:
        self.todos.drain()
        return []

    def stats(self) -> dict:
        return {"activations": self.activations, "cycle_failures": self.cycle_failures, "pending_todos": self.todos.pending_count(),
                "dropped": self.dropped, "last_error": self.last_error}

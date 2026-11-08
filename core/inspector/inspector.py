"""点検係の本体（S-13.2 の段、D-12）。時計と購読で状態を 1 歩ずつ進める。1 回の tick() は短く、待たない。

状態（state.phase）: idle → notice → wait（減らし込みを出した後。段 2 と 3）→ review_request（段 4〜7 を管理役に頼んだ）→ idle（段 8 の後）。
- 段 1 予告: next_at の inspection_notice_min 分前に inspection.phase=notice。
- 段 2 減らし込み: 全プラグインに set_mode draining を 1 つずつ（issued_by=inspection、on_expiry=release、期限 drain_max_h）。
  期限で必ず解除されるので、点検係が落ちても点検中のまま残らない。
- 段 3 待つ: 時系列記録の projections（runs と jobs の active=true の行）が 0 になるか drain_wait_h に達するまで。達しても長い作業は止めない。
- 段 4〜7: inspection.phase=review_request を出して管理役に頼む。管理役が manager.decision(step=review) を返すまで待つ。
  期限（drain_max_h）までに返らなければ、見直しは次回へ回して段 8 へ進む。
- 点検中の沈黙: オブザーバーの health.alert(check=drain_silent、宛先 inspection) を受けたら、段 4〜7 を中止して段 8 へ（S-13.3）。
- 段 8 再開: 点検係の命令をすべて解除する。
- 間隔（S-13.1）: 前の inspection_quiet_count 回が静かなら inspection_stretch 倍（上限まで）。崩れが出たら初期値に戻す。
  「静か」= 恒久修繕の ToDo が 0 件、予備役の起動が 0 回、見直しを飛ばしていない。
点検係は中核なので、制御命令では変えない（制御 API の対象にならない）。届かないもの（制御 API・時系列記録）があっても、点検係は止まらない。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from axcommon import ids, timeutil
from axcommon.client import ApiError, Unavailable
from axcommon.events import EventSink

from . import (COMPONENT, PHASE_DRAINING, PHASE_NOTICE, PHASE_RESUME, PHASE_REVIEW, PHASE_REVIEW_REQUEST, PHASE_SCHEDULE, PHASE_WAIT)
from .config import InspectionConfig
from .state import StateStore

log = logging.getLogger("ax.inspector")
WATCH_KINDS = ("health.alert", "manager.decision", "backup.activated")


class Inspector:
    def __init__(self, clients, store: StateStore, sink: EventSink, cfg: InspectionConfig):
        """clients: timeline（TimelineClient 互換）と control（commands/issue/release/plugins を持つ）。"""
        self.c, self.store, self.sink, self.cfg = clients, store, sink, cfg
        self.timeline_ok = True
        self.state = store.load() or self._initial_state()
        # 利用者が範囲を狭めた後の再起動でも、保存済みの間隔を範囲に収める。
        before = self.state["interval_h"]
        self.state["interval_h"] = cfg.clamp(before)
        if before != self.state["interval_h"]:
            self._phase_event(PHASE_SCHEDULE, {"previous_interval_h": before, "interval_h": self.state["interval_h"],
                                                "next_at": self.state["next_at"], "clamped": True})
        self.store.save(self.state)

    # ---------------- 状態 ----------------
    def now(self):
        return timeutil.now()

    def _initial_state(self) -> dict:
        now = self.now()
        nxt = self.cfg.first_at or timeutil.format_ts(now + timedelta(hours=self.cfg.initial_h))
        return {"interval_h": self.cfg.initial_h, "next_at": nxt, "quiet_streak": 0, "phase": "idle", "inspection_id": None, "started_at": None,
                "drain_started_at": None, "command_ids": [], "review_requested_at": None, "skip_review": False, "changes": [],
                "repairs": 0, "backup_activations": 0, "event_cursor": 0, "last_result": None, "review_done": False}

    def _save(self) -> None:
        self.store.save(self.state)

    def _phase_event(self, phase: str, plan: dict | None = None) -> None:
        plan = {"inspection_id": self.state.get("inspection_id"), **(plan or {})}
        self.sink.emit("inspection.phase", COMPONENT, {"phase": phase, "plan": plan})

    # ---------------- 購読 ----------------
    def poll(self) -> int:
        """health.alert（drain_silent）、manager.decision（見直しの返り）、backup.activated を読む。"""
        try:
            r = self.c.timeline.get_events(after=self.state["event_cursor"], kind=",".join(WATCH_KINDS), limit=200) or {}
        except (Unavailable, ApiError):
            self.timeline_ok = False
            return 0
        self.timeline_ok = True
        n = 0
        for ev in r.get("events") or []:
            n += 1 if self._on_event(ev) else 0
        nxt = r.get("next_after")
        if isinstance(nxt, int) and nxt > self.state["event_cursor"]:
            self.state["event_cursor"] = nxt
        elif r.get("events"):
            self.state["event_cursor"] = max(self.state["event_cursor"], max(int(e.get("seq") or 0) for e in r["events"]))
        self._save()
        return n

    def _on_event(self, ev: dict) -> bool:
        kind, data = ev.get("kind"), ev.get("data") if isinstance(ev.get("data"), dict) else {}
        s = self.state
        if kind == "backup.activated":
            s["backup_activations"] += 1            # 前回の点検の再開からの分を数える（静かさの判定。再開で 0 に戻す）
            return True
        if kind == "health.alert" and data.get("destination") == "inspection" and data.get("check") == "drain_silent":
            if s["phase"] in ("wait", "review_request"):
                s["skip_review"] = True              # S-13.3: 段 4〜7 を飛ばして段 8 を行う
                return True
            return False
        if kind == "manager.decision" and data.get("step") == "review" and s["phase"] == "review_request":
            if data.get("inspection_id") == s.get("inspection_id"):
                s["review_done"] = True
                s["repairs"] = int(data.get("repairs") or 0)
                s["review_report"] = {"steps": data.get("steps"), "backup_activations": data.get("backup_activations")}
                return True
        return False

    # ---------------- 1 歩 ----------------
    def run_once(self) -> list[str]:
        self.poll()
        return self.tick()

    def tick(self) -> list[str]:
        """時計と状態を見て、進められるだけ進める（待たない）。起きたことの名前を返す。"""
        did: list[str] = []
        for _ in range(8):         # 1 回の tick で連続して進められる段の上限（無限ループの防止）
            step = self._advance()
            if not step:
                break
            did.append(step)
        return did

    def _advance(self) -> str | None:
        s, now = self.state, self.now()
        phase = s["phase"]
        if phase == "idle":
            if now >= timeutil.parse_ts(s["next_at"]) - timedelta(minutes=self.cfg.notice_min):
                return self._notice()
            return None
        if phase == "notice":
            if now >= timeutil.parse_ts(s["next_at"]):
                if now >= timeutil.parse_ts(s["next_at"]) + timedelta(hours=self.cfg.drain_wait_h):
                    return self._resume("control_unavailable")      # 制御 API に届かないまま待った。今回は見送る
                return self._drain()
            return None
        if phase == "wait":
            if s["skip_review"] or now >= self._drain_deadline():
                return self._resume("silent" if s["skip_review"] else "drain_max")
            if self._active_count() == 0 or now >= timeutil.parse_ts(s["drain_started_at"]) + timedelta(hours=self.cfg.drain_wait_h):
                return self._request_review()
            return None
        if phase == "review_request":
            if s["skip_review"]:
                return self._resume("silent")
            if s["review_done"]:
                return self._review_done()
            if now >= self._drain_deadline():
                return self._resume("review_timeout")      # 見直しは次回へ回す
            return None
        return None

    def _drain_deadline(self):
        return timeutil.parse_ts(self.state["drain_started_at"]) + timedelta(hours=self.cfg.drain_max_h)

    # 段 1
    def _notice(self) -> str:
        s = self.state
        s.update(phase="notice", inspection_id=ids.new_ulid(), started_at=timeutil.now_ts(), skip_review=False, review_done=False,
                 repairs=0, command_ids=[], drain_started_at=None, review_report=None)
        self._phase_event(PHASE_NOTICE, {"start_at": s["next_at"], "interval_h": s["interval_h"]})
        self._save()
        return PHASE_NOTICE

    # 段 2
    def _drain(self) -> str | None:
        s, now = self.state, self.now()
        try:
            plugins = [p["id"] for p in (self.c.control.plugins() or {}).get("plugins") or []]
        except (Unavailable, ApiError):
            return None            # 制御 API に届かない。何も出さずに次の tick でやり直す（点検係は止まらない）
        s["drain_started_at"] = timeutil.format_ts(now)
        expires = timeutil.format_ts(now + timedelta(hours=self.cfg.drain_max_h))
        issued, failed = [], []
        for pid in plugins:
            try:
                r = self.c.control.issue({"target": pid, "op": "set_mode", "args": {"mode": "draining"}, "expires_at": expires,
                                          "on_expiry": "release", "reason": f"定期点検 {s['inspection_id']} の減らし込み（段 2）"})
                issued.append((r or {}).get("id"))
            except (Unavailable, ApiError) as e:
                failed.append(f"{pid}:{getattr(e, 'code', 'unavailable')}")      # 1 つの失敗で他を止めない
        s["command_ids"] = [i for i in issued if i]
        s["phase"] = "wait"
        self._phase_event(PHASE_DRAINING, {"commands": len(s["command_ids"]), "failed": failed, "expires_at": expires})
        self._phase_event(PHASE_WAIT, {"active": None, "drain_wait_h": self.cfg.drain_wait_h})
        self._save()
        return PHASE_DRAINING

    # 段 3
    def _active_count(self) -> int:
        n = 0
        for name in ("runs", "jobs"):
            try:
                r = self.c.timeline.projection(name, active="true") or {}
            except (Unavailable, ApiError):
                return 1          # 数えられないときは「まだ動いている」とみなす（早まって見直しに進まない。上限で必ず進む）
            n += len(r.get("rows") or [])
        return n

    # 段 4〜7 を頼む
    def _request_review(self) -> str:
        s = self.state
        s.update(phase="review_request", review_requested_at=timeutil.now_ts())
        self._phase_event(PHASE_REVIEW_REQUEST, {"since": s.get("started_at"), "active": self._active_count()})
        self._save()
        return PHASE_REVIEW_REQUEST

    def _review_done(self) -> str:
        s = self.state
        changes, s["changes"] = list(s.get("changes") or []), []
        self._phase_event(PHASE_REVIEW, {"changes": changes, "repairs": s["repairs"], "backup_activations": s["backup_activations"],
                                         "manager": s.get("review_report")})
        return self._resume("done")

    # 段 8
    def _resume(self, why: str) -> str:
        s = self.state
        released, failed = self._release_all()
        review_ok = why == "done"
        if not review_ok and (s.get("changes")):
            # 見直しを飛ばした回の利用者変更の記録は、飛ばしても残す（S-13.2）
            self._phase_event(PHASE_REVIEW, {"changes": s["changes"], "skipped": why})
            s["changes"] = []
        quiet = review_ok and s["repairs"] == 0 and s["backup_activations"] == 0
        before = s["interval_h"]
        if quiet:
            s["quiet_streak"] += 1
            if s["quiet_streak"] >= self.cfg.quiet_count:
                s["interval_h"] = self.cfg.clamp(s["interval_h"] * self.cfg.stretch)
        else:
            s["quiet_streak"] = 0
            s["interval_h"] = self.cfg.clamp(self.cfg.initial_h)
        result = {"inspection_id": s["inspection_id"], "resume_reason": why, "quiet": quiet, "repairs": s["repairs"],
                  "backup_activations": s["backup_activations"], "released": released, "release_failed": failed,
                  "interval_before_h": before, "interval_after_h": s["interval_h"]}
        self._phase_event(PHASE_RESUME, {"reason": why, "released": released, "release_failed": failed})
        s.update(phase="idle", next_at=timeutil.format_ts(self.now() + timedelta(hours=s["interval_h"])), command_ids=[], last_result=result,
                 skip_review=False, review_done=False, repairs=0, backup_activations=0)
        self._phase_event(PHASE_SCHEDULE, {"interval_h": s["interval_h"], "next_at": s["next_at"], "quiet_streak": s["quiet_streak"],
                                           "previous_interval_h": before, "quiet": quiet, "clamped": self.cfg.clamped})
        self.store.append_history(result)
        s["inspection_id"] = None
        self._save()
        return PHASE_RESUME

    def _release_all(self) -> tuple[int, list[str]]:
        """点検係の命令をすべて解除する（自分の記録と、制御 API の issued_by=inspection の両方）。"""
        ids_ = list(self.state.get("command_ids") or [])
        try:
            for st in ("active", "expired_pending"):
                ids_ += [c["id"] for c in self.c.control.commands(state=st, issued_by="inspection")]
        except (Unavailable, ApiError):
            pass
        done, failed = 0, []
        for cid in dict.fromkeys(ids_):
            try:
                self.c.control.release(cid, "定期点検の再開（段 8）")
                done += 1
            except ApiError as e:
                if e.status not in (404, 409):          # 既に解除済みは成功とみなす
                    failed.append(f"{cid}:{e.code}")
            except Unavailable:
                failed.append(f"{cid}:unavailable")
        return done, failed

    # ---------------- 利用者の変更の記録（D-12）----------------
    def record_user_change(self, changes: list[dict]) -> dict:
        """役割の候補などを利用者が変えたときの「前の値・新しい値・理由」。次の review の記録に載せる（点検の外で review を書くと
        オブザーバーが減らし込みと取り違えるため。DECISIONS）。"""
        clean = []
        for c in changes:
            if not isinstance(c, dict) or not {"key", "before", "after", "reason"} <= set(c):
                raise ValueError("changes の各項目は key、before、after、reason を持つ")
            if not isinstance(c["reason"], str) or not c["reason"].strip():
                raise ValueError("changes の reason は空でない文字列にする")
            clean.append({"key": c["key"], "before": c["before"], "after": c["after"], "reason": c["reason"]})
        self.state.setdefault("changes", []).extend(clean)
        self._save()
        return {"recorded": len(clean), "pending": len(self.state["changes"])}

    def view(self) -> dict:
        s = self.state
        return {k: s.get(k) for k in ("phase", "inspection_id", "interval_h", "next_at", "quiet_streak", "last_result")} | {
            "pending_changes": len(s.get("changes") or []), "timeline_ok": self.timeline_ok}

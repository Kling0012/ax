"""台帳の業務のまとめ（S-9、D-8）。Starlette の口（app.py）と、このservice の間の境界。

- 遷移・依存・カード（S-9.2、S-9.3）
- 集中状態・日次状態（S-9.5。as_of を版として全置換）
- 目標・約束・予定（S-9.4）
- 判断の記録（S-9.6、D-8）
- parked の走査（check_at 超過。D-8）と item.state の購読（S-9.3、D-8）

秘密検査（S-15.3）は axcommon.redact（B1 の束）から動的に読む。使えないときは対象の書き込みだけ503にする。
試験は偽の検査器を注入する。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from axcommon import ids, schemas, timeutil
from axcommon.defaults import Defaults
from axcommon.events import EventSink

from . import HIBI_STATE_PLUGIN, TRANSITIONS, USER_ONLY_REASONS, WAITING_REASONS, Reject, user_only_for
from .extschemas import validate_decision, validate_item
from .store import Store

log = logging.getLogger("core.ledger")

TOKYO = ZoneInfo("Asia/Tokyo")
ITEM_STATES_FOLLOWED = {"completed", "ejected"}


def default_secret_scan(value: Any) -> list[str]:
    """S-15.3 の秘密検査（axcommon.redact。B1 の束）。本文全体の文字列を1つずつ `Redactor.scan` に通す。
    戻り値は当たった場所（JSON の経路）の一覧で、値は含めない。読めなければ書き込みを503にする。"""
    try:
        from axcommon.redact import from_environment
    except Exception:  # noqa: BLE001
        raise Reject(503, "secret_scan_unavailable", "秘密検査器を読めない") from None
    try:
        red = from_environment()
    except Exception:  # noqa: BLE001
        raise Reject(503, "secret_scan_unavailable", "秘密検査器を作れない") from None
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            if red.contains_secret(node):
                found.append(path)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(value, "")
    return found


class LedgerService:
    def __init__(self, store: Store, sink: EventSink | None, *, defaults: Defaults | None = None,
                 secret_scan: Callable[[Any], list[str]] | None = None):
        self.store = store
        self.sink = sink
        self.defaults = defaults or Defaults.load()
        self.secret_scan = secret_scan or default_secret_scan
        self.secret_scan_available = self._probe_secret_scan()

    def _probe_secret_scan(self) -> bool:
        try:
            from axcommon.redact import from_environment  # noqa: F401

            return True
        except Exception:  # noqa: BLE001
            return False

    # ---- 事象 ----
    def emit(self, kind: str, subject: str, data: dict, **kw) -> None:
        if self.sink is None:
            return
        try:
            self.sink.emit(kind, subject, data, **kw)
        except Exception:  # noqa: BLE001  記録の失敗で台帳を止めない（S-2.2）
            log.exception("事象 %s を送れない", kind)

    def _task_state(self, todo: dict, reason: str | None, extra: dict | None = None) -> None:
        data: dict[str, Any] = {"todo_id": todo["id"], "state": todo["state"], "reason": reason}
        if extra:
            data.update(extra)
        schemas.validate("event-data", data, fragment="/$defs/task.state")
        self.emit("task.state", todo["id"], data)

    # ---- ToDo: 作成（S-9.1〜S-9.3）----
    def create_todo(self, body: dict, *, actor: str, via: str | None = None) -> dict:
        if not isinstance(body, dict):
            raise Reject(422, "bad_request", "本文が JSON の物でない")
        todo = dict(body)
        if not isinstance(todo.get("title"), str):
            raise Reject(422, "bad_request", "title が無い")
        todo.setdefault("id", ids.new_ulid())
        if self.store.get_todo(todo["id"]) is not None:
            raise Reject(409, "already_exists", "同じ ID の ToDo がある")
        todo.setdefault("state", "todo")
        todo.setdefault("owner", "system")
        if "user_only" in todo:
            declared = bool(todo["user_only"])
        else:
            declared = None
        card = todo.get("card")
        reason_cat = (card or {}).get("waiting_reason") if isinstance(card, dict) else None
        if todo["state"] == "waiting_user":
            self._validate_waiting(card, reason_cat, declared)
            todo["card"] = self._fix_card(card, reason_cat, force_user_only=user_only_for(reason_cat),
                                          declared=declared)
            todo["user_only"] = todo["card"]["user_only"]
        else:
            todo["user_only"] = bool(declared) if declared is not None else False
            if todo["state"] == "parked":
                if not todo.get("reason"):
                    raise Reject(422, "reason_required", "parked には reason が要る（S-9.2）")
                if not todo.get("check_at"):
                    raise Reject(422, "check_at_required", "parked には check_at が要る（S-9.2）")
            if todo["state"] == "dropped" and not todo.get("reason"):
                raise Reject(422, "reason_required", "dropped には理由が要る（S-9.2）")
        if errs := schemas.errors("todo", todo):
            raise Reject(422, "bad_request", "; ".join(errs[:5]))
        now = timeutil.now_ts()
        todo.setdefault("created_at", now)
        todo.setdefault("updated_at", now)
        todo["created_by"] = actor
        todo["due_notified"] = False
        self.store.put_todo(todo, actor=actor, kind_of_change="created", before=None)
        self.store.replace_deps(todo["id"], todo.get("depends_on") or [])
        self._task_state(todo, todo.get("reason"))
        return todo

    @staticmethod
    def _validate_waiting(card: Any, reason_cat: str | None, declared_user_only: bool | None) -> None:
        if not isinstance(card, dict) or not (card.get("question") or "").strip():
            raise Reject(422, "card_required", "waiting_user には質問のカードが要る（S-9.2）")
        if reason_cat not in WAITING_REASONS:
            raise Reject(422, "waiting_reason_required",
                         "waiting_user の理由は 7 種のどれか（S-9.2）: " + ",".join(sorted(WAITING_REASONS)))
        forced = reason_cat in USER_ONLY_REASONS or card.get("kind") == "approval"
        if forced and (declared_user_only is False or card.get("user_only") is False):
            raise Reject(422, "user_only_fixed", "このカードの user_only は true 固定（S-9.2、D-8）")
        if isinstance(card, dict) and card.get("user_only") is True and declared_user_only is False:
            raise Reject(422, "user_only_fixed", "user_only は true から false へ戻せない（S-9.2）")

    @staticmethod
    def _fix_card(card: dict, reason_cat: str, *, force_user_only: bool, declared: bool | None) -> dict:
        fixed = dict(card)
        fixed["waiting_reason"] = reason_cat
        fixed.setdefault("options", [])
        fixed.setdefault("answer", None)
        fixed.setdefault("version", 1)
        fixed.setdefault("notification_state", "not_notified")
        fixed["user_only"] = force_user_only or card.get("kind") == "approval" or bool(card.get("user_only")) or bool(declared)
        return fixed

    # ---- ToDo: 読み取り ----
    def get_todo(self, todo_id: str, *, with_history: bool = False) -> dict | None:
        todo = self.store.get_todo(todo_id)
        if todo is None:
            return None
        if with_history:
            todo["history"] = self.store.todo_history(todo_id)
        return todo

    def list_todos(self, *, state: str | None = None, project: str | None = None, subloop: str | None = None,
                   due: bool = False, owner: str | None = None) -> list[dict]:
        where, params = [], []
        if state:
            where.append("state=?")
            params.append(state)
        if project:
            where.append("project=?")
            params.append(project)
        if subloop:
            where.append("subloop=?")
            params.append(subloop)
        if owner:
            where.append("owner=?")
            params.append(owner)
        if due:
            where.append("due_at IS NOT NULL AND due_at <= ? AND state NOT IN ('done','dropped')")
            params.append(timeutil.now_ts())
        w = ("WHERE " + " AND ".join(where)) if where else ""
        return self.store.list_todos(w, params)

    # ---- ToDo: 遷移（S-9.2、S-9.3）----
    def record_work_request(self, todo_id: str, body: dict, *, actor: str, via: str | None) -> dict:
        todo = self.store.get_todo(todo_id)
        if todo is None:
            raise Reject(404, "not_found", "ToDo not found")
        if (set(body) - {"operation", "principal", "reason", "job_id"}
                or body.get("operation") not in {"work.stop", "work.cancel"}
                or (body.get("operation") == "work.cancel" and not body.get("reason"))
                or not isinstance(body.get("principal"), str) or not body["principal"]
                or any(not isinstance(body[k], str) or not body[k] for k in ("reason", "job_id") if k in body)):
            raise Reject(422, "bad_request", "invalid work request")
        if (body["operation"] == "work.cancel" and todo["state"] == "waiting_user"
                and todo.get("user_only") and via != "hq"):
            raise Reject(403, "user_only_card", "user-only card")
        self.store.record_work_request(todo_id, {**body, "via": via}, actor=actor, ts=timeutil.now_ts())
        return {"recorded": True}

    def transition(self, todo_id: str, body: dict, *, actor: str, via: str | None = None) -> dict:
        to = body.get("to")
        reason = body.get("reason")
        with self.store.transaction():
            todo = self.store.get_todo(todo_id)
            if todo is None:
                raise Reject(404, "not_found", f"ToDo {todo_id} が無い")
            if errs := schemas.errors("todo-transition", body):
                raise Reject(422, "bad_request", "; ".join(errs[:5]))
            expected = body.get("expected_history_seq")
            if expected is not None and self.store.latest_todo_history_seq(todo_id) != expected:
                raise Reject(409, "stale_version", "ToDo has changed")
            cur = todo["state"]
            # user_only のカードは、表の検証の前に hq 以外の完了・保留・中止を拒否する（S-9.2。回答を飛ばさせない）
            if cur == "waiting_user" and todo.get("user_only") and to in {"done", "parked", "dropped"} and via != "hq":
                raise Reject(403, "user_only_card",
                             "user_only のカードは hq 以外からは完了・保留・中止にできない（S-9.2）")
            if to not in TRANSITIONS.get(cur, set()):
                raise Reject(409, "invalid_transition", f"{cur} → {to} は遷移の表にない（S-9.2）")
            if cur == "waiting_user" and to == "todo":
                raise Reject(409, "answer_required", "waiting_user → todo は回答（/answer）と一体でのみ行う（S-9.3）")
            after = dict(todo)
            after["state"] = to
            after["updated_at"] = timeutil.now_ts()
            if to == "waiting_user":
                card = todo.get("card") if isinstance(todo.get("card"), dict) else {}
                reason_cat = reason if reason in WAITING_REASONS else None
                if reason_cat is None:
                    reason_cat = card.get("waiting_reason")
                declared = card.get("user_only") if "user_only" in card else (True if todo.get("user_only") else None)
                self._validate_waiting(card, reason_cat, declared)
                after["card"] = self._fix_card(card, reason_cat, force_user_only=user_only_for(reason_cat), declared=declared)
                after["user_only"] = after["card"]["user_only"]
                after["reason"] = reason_cat
            if to == "parked":
                if not reason:
                    raise Reject(422, "reason_required", "parked には reason が要る（S-9.2）")
                after["reason"] = reason
                if body.get("check_at"):
                    after["check_at"] = body["check_at"]
                if not after.get("check_at"):
                    raise Reject(422, "check_at_required", "parked には check_at が要る（S-9.2）")
            if to == "dropped":
                if not reason:
                    raise Reject(422, "reason_required", "dropped には理由が要る（S-9.2）")
                after["reason"] = reason
                after["cancel_reason"] = reason
            if to == "running":
                after["reason"] = reason
            after["due_notified"] = False
            self.store.put_todo(after, actor=actor, kind_of_change=f"transition:{cur}->{to}", before=todo)
        self._task_state(after, reason)
        if to in {"parked", "dropped"}:
            self._cascade_dependents(todo_id, actor, cause=to)
        return after

    def _cascade_dependents(self, todo_id: str, actor: str, *, cause: str) -> None:
        """依存先が parked/dropped になったら、依存する ToDo を parked(dependency) にする（S-9.2、D-8）。"""
        max_hops = int(self.defaults.get("max_hops"))
        wait_h = float(self.defaults.get("dependency_wait_h"))
        seen: set[str] = set()
        frontier = [todo_id]
        hops = 0
        while frontier:
            hops += 1
            if hops > max_hops:
                self.emit("error.classified", todo_id,
                          {"class": "unknown", "code": "dependency_chain_too_long",
                           "message": f"依存の連鎖が max_hops={max_hops} を超えた（D-8）"})
                return
            nxt: list[str] = []
            for dep in frontier:
                for tid in self.store.dependents_of(dep):
                    if tid in seen:
                        continue
                    seen.add(tid)
                    todo = self.store.get_todo(tid)
                    if todo is None or todo["state"] in {"done", "dropped", "parked"}:
                        continue
                    after = dict(todo)
                    after["state"] = "parked"
                    after["reason"] = "dependency"
                    after["check_at"] = timeutil.format_ts(timeutil.now() + timedelta(hours=wait_h))
                    after["updated_at"] = timeutil.now_ts()
                    after["due_notified"] = False
                    self.store.put_todo(after, actor="ledger", kind_of_change="transition:dependency", before=todo)
                    self._task_state(after, "dependency", {"dependency": dep, "cause": cause})
                    nxt.append(tid)
            frontier = nxt

    # ---- ToDo: 回答（S-9.2、S-9.3、S-22.3）----
    def answer(self, todo_id: str, body: dict, *, actor: str, via: str | None = None) -> dict:
        if errs := schemas.errors("todo-answer", body):
            raise Reject(422, "bad_request", "; ".join(errs[:5]))
        todo = self.store.get_todo(todo_id)
        if todo is None:
            raise Reject(404, "not_found", f"ToDo {todo_id} が無い")
        card = dict(todo.get("card") or {})
        if card.get("answer") is not None:
            raise Reject(409, "already_answered", "回答済みのカード（S-9.3）")
        if todo["state"] != "waiting_user":
            raise Reject(409, "not_waiting", f"状態が {todo['state']}。回答は waiting_user だけ")
        version = body.get("version")
        if version is not None and version != card.get("version", 1):
            raise Reject(409, "version_mismatch", "質問の版が一致しない（S-9.3）")
        source = body.get("answer_source", "hq")
        user_only = bool(todo.get("user_only"))
        if user_only and (source != "hq" or via != "hq"):
            raise Reject(403, "user_only_card", "user_only のカードは HQ の受け口だけが回答できる（S-9.2）")
        if source == "relay":
            if not user_only and not (body.get("quote") or "").strip():
                raise Reject(422, "quote_required", "中継の回答には quote が要る（S-9.2）")
        new_card = dict(card)
        new_card["answer"] = {
            "answer": body["answer"], "answer_source": source,
            **({"quote": body["quote"]} if body.get("quote") else {}),
            "answered_by": actor, "answered_at": timeutil.now_ts(),
        }
        after = dict(todo)
        after["card"] = new_card
        after["state"] = "todo"
        after["updated_at"] = timeutil.now_ts()
        after["due_notified"] = False
        # 回答と waiting_user → todo の遷移を一体として記録する（S-9.3）
        self.store.put_todo(after, actor=actor, kind_of_change="answer", before=todo)
        self._task_state(after, "answered", {"answer_source": source})
        return after

    # ---- parked の走査（D-8）----
    def sweep_parked(self) -> int:
        """check_at を過ぎた parked に task.state（data.due=true）を1回だけ書く。書いた件数を返す。"""
        now = timeutil.now_ts()
        count = 0
        for todo in self.store.list_todos("WHERE state='parked' AND check_at IS NOT NULL AND check_at <= ? AND due_notified=0", [now]):
            after = dict(todo)
            after["due_notified"] = True
            after["updated_at"] = now
            self.store.put_todo(after, actor="ledger", kind_of_change="due", before=todo)
            self._task_state(after, "due", {"due": True})
            count += 1
        return count

    # ---- item.state の購読（S-9.3、D-8）----
    def handle_item_events(self, events: list[dict]) -> list[str]:
        """時系列記録から受け取った item.state の事象を処理する。処理した todo_id の一覧を返す。"""
        done: list[str] = []
        for ev in events or []:
            data = ev.get("data") or {}
            todo_id = data.get("todo_id")
            state = data.get("state")
            if not todo_id or state not in ITEM_STATES_FOLLOWED:
                continue
            todo = self.store.get_todo(todo_id)
            if todo is None or todo["state"] != "running":
                continue
            if state == "completed":
                if todo.get("item_kind") != "work":
                    continue  # kind: work 以外は触らない（入口が仕上げる。S-9.3）
                after = dict(todo)
                after["state"] = "review"
                arts = list(todo.get("artifacts") or [])
                arts += [a for a in (data.get("artifacts") or []) if a not in arts]
                after["artifacts"] = arts
                after["updated_at"] = timeutil.now_ts()
                self.store.put_todo(after, actor="ledger", kind_of_change="item_completed", before=todo)
                self._task_state(after, data.get("reason"), {"item_id": data.get("item_id"), "artifacts": arts})
            else:  # ejected
                after = dict(todo)
                after["state"] = "parked"
                after["reason"] = data.get("reason") or "ejected"
                after["check_at"] = timeutil.format_ts(timeutil.now() + timedelta(days=1))
                src = dict(todo.get("source") or {})
                if data.get("item_id"):
                    src["item_id"] = data["item_id"]
                run_id = ev.get("run_id") or data.get("run_id")
                if run_id:
                    src["run_id"] = run_id
                if ev.get("id"):
                    src["event_id"] = ev["id"]
                after["source"] = src
                after["due_notified"] = False
                after["updated_at"] = timeutil.now_ts()
                self.store.put_todo(after, actor="ledger", kind_of_change="item_ejected", before=todo)
                self._task_state(after, after["reason"], {"item_id": data.get("item_id")})
                self._cascade_dependents(todo_id, "ledger", cause="parked")
            done.append(todo_id)
        return done

    # ---- 集中状態・日次状態（S-9.5）----
    def _jst_today(self) -> str:
        return timeutil.now().astimezone(TOKYO).strftime("%Y-%m-%d")

    def _yesterday(self) -> str:
        return (timeutil.now().astimezone(TOKYO) - timedelta(days=1)).strftime("%Y-%m-%d")

    def get_focus(self) -> dict:
        body, as_of, version = self.store.get_focus()
        if body is None:
            return {"state": "unknown", "as_of": None, "version": 0}
        return {"state": body.get("state"), "since": body.get("since"), "as_of": as_of, "version": version}

    def get_daily(self, date: str) -> dict:
        body, as_of, version = self.store.get_daily(date)
        if body is None:
            return {"date": date, "as_of": None, "version": 0, "segments": []}
        return {**body, "as_of": as_of, "version": version}

    def _check_state_body(self, kind: str, body: Any, *, body_bytes: int) -> None:
        if not isinstance(body, dict):
            raise Reject(422, "schema", "本文が JSON の物でない")
        if kind == "focus":
            if isinstance(body.get("state"), str) and body["state"] == "unknown":
                raise Reject(422, "unknown_not_recorded", "unknown は書かない（S-9.5）")
            errs = schemas.errors("focus-state", body)
        else:
            errs = schemas.errors("daily-state", body)
        if errs:
            raise Reject(422, "schema", "; ".join(errs[:5]))
        try:
            timeutil.parse_ts(body["as_of"])
            if kind == "focus":
                timeutil.parse_ts(body["since"])
            else:
                for segment in body["segments"]:
                    timeutil.parse_ts(segment["start"])
                    timeutil.parse_ts(segment["end"])
        except (ValueError, TypeError):
            raise Reject(422, "schema", "日時が不正（S-9.5）") from None
        if kind == "daily":
            limit = int(self.defaults.get("hibi_segments_max"))
            if len(body.get("segments") or []) > limit:
                raise Reject(422, "segments_max", f"segments が {limit} 件を超える（S-17 hibi_segments_max）")
        max_kib = int(self.defaults.get("hibi_body_max_kib"))
        if body_bytes > max_kib * 1024:
            raise Reject(413, "too_large", f"本文が {max_kib} KiB を超える（S-17 hibi_body_max_kib）")

    def put_focus(self, body: Any, *, issuer: str, body_bytes: int | None = None) -> dict:
        raw = _body_size(body, body_bytes)
        self._check_state_body("focus", body, body_bytes=raw)
        return self._put_state("focus", None, body, issuer=issuer)

    def put_daily(self, date: str, body: Any, *, issuer: str, body_bytes: int | None = None) -> dict:
        raw = _body_size(body, body_bytes)
        self._check_state_body("daily", body, body_bytes=raw)
        if body.get("date") != date:
            raise Reject(422, "date_mismatch", "パスと本文の date が違う（S-9.5）")
        today, yesterday = self._jst_today(), self._yesterday()
        if date not in {today, yesterday}:
            raise Reject(422, "date_out_of_window", "書ける日付は今日と昨日（Asia/Tokyo。S-9.5）")
        return self._put_state("daily", date, body, issuer=issuer)

    def _put_state(self, kind: str, date: str | None, body: dict, *, issuer: str) -> dict:
        as_of = timeutil.parse_ts(body["as_of"])
        skew_min = float(self.defaults.get("hibi_future_skew_min"))
        if as_of > timeutil.now() + timedelta(minutes=skew_min):
            raise Reject(422, "as_of_in_future", f"as_of が現在より {skew_min} 分より先（S-17 hibi_future_skew_min）")
        if hits := self.secret_scan(body):
            raise Reject(422, "secret_detected", f"本文に秘密検査に当たる値がある（S-15.3）: {len(hits)} 件")
        try:
            with self.store.transaction():
                version_key = "focus" if kind == "focus" else date
                if kind == "focus":
                    stored, stored_as_of, stored_version = self.store.get_focus()
                else:
                    stored, stored_as_of, stored_version = self.store.get_daily(version_key)
                if stored is not None:
                    if as_of < timeutil.parse_ts(stored_as_of):
                        raise Reject(409, "stale_as_of", "as_of が保存済みより古い（S-9.5）")
                    if as_of == timeutil.parse_ts(stored_as_of):
                        if _same_content(stored, body):
                            return {"stored": False, "kind": kind, "as_of": stored_as_of, "version": stored_version}
                        raise Reject(409, "as_of_conflict", "同じ as_of で内容が違う（S-9.5）")
                now = timeutil.now_ts()
                version = stored_version + 1
                if kind == "focus":
                    self.store.put_focus(body, body["as_of"], version, now)
                else:
                    self.store.put_daily(version_key, body, body["as_of"], version, now)
                self.store.add_state_history(kind, date, now, issuer, stored, body)
        except sqlite3.Error:
            raise Reject(503, "storage_unavailable", "状態を保存できない") from None
        # 内容が変わった書き込みだけ state.recorded を書く（S-9.5）
        ev_data = {"kind": kind, "as_of": body["as_of"], "issuer": issuer, "changed": True}
        if date:
            ev_data["date"] = date
        if kind == "focus":
            ev_data["state"] = body.get("state")
        schemas.validate("event-data", ev_data, fragment="/$defs/state.recorded")
        self.emit("state.recorded", f"state-{kind}", ev_data)
        return {"stored": True, "kind": kind, "as_of": body["as_of"], "version": version}

    def state_history(self, kind: str, date: str | None = None) -> list[dict]:
        return self.store.state_history(kind, date)

    # ---- 目標・約束・予定（S-9.4、S-9.5）----
    ITEM_STATES = {
        "goal": ({"active", "done", "dropped"}, ("active",)),
        "promise": ({"open", "done", "cancelled"}, ("open",)),
        "schedule": ({"planned", "done", "cancelled"}, ("planned",)),
    }

    def create_item(self, kind: str, body: dict, *, actor: str) -> dict:
        if not isinstance(body, dict):
            raise Reject(422, "schema", "本文が JSON の物でない")
        item = dict(body)
        item.setdefault("id", ids.new_ulid())
        if self.store.get_item(kind, item["id"]) is not None:
            raise Reject(409, "already_exists", "同じ ID の記録がある")
        item["version"] = 1
        _, initial = self.ITEM_STATES[kind]
        item.setdefault("state", initial[0])
        if errs := validate_item(kind, item):
            raise Reject(422, "schema", "; ".join(errs[:5]))
        now = timeutil.now_ts()
        item.setdefault("created_at", now)
        item.setdefault("updated_at", now)
        if kind == "schedule":
            if timeutil.parse_ts(item["end_at"]) < timeutil.parse_ts(item["start_at"]):
                raise Reject(422, "end_before_start", "予定の終了は開始以降（S-9.4）")
        self.store.put_item(kind, item, actor=actor, kind_of_change="created", before=None)
        return item

    def get_item(self, kind: str, item_id: str, *, with_history: bool = False) -> dict | None:
        item = self.store.get_item(kind, item_id)
        if item is not None and with_history:
            item["history"] = self.store.item_history(kind, item_id)
        return item

    def list_items(self, kind: str, params: dict) -> list[dict]:
        where, args = [], []
        if kind == "promise":
            for key in ("todo_id", "state"):
                if params.get(key):
                    where.append(f"{key}=?")
                    args.append(params[key])
            if params.get("due_by"):
                where.append("due_at<=?")
                args.append(params["due_by"])
            elif params.get("due") in ("1", 1, True):
                where.append("due_at<=? AND state='open'")
                args.append(timeutil.now_ts())
        if kind == "schedule":
            if params.get("state"):
                where.append("state=?")
                args.append(params["state"])
            if params.get("from"):
                where.append("julianday(json_extract(body, '$.end_at'))>=julianday(?)")
                args.append(params["from"])
            if params.get("to"):
                where.append("julianday(json_extract(body, '$.start_at'))<=julianday(?)")
                args.append(params["to"])
        if kind == "goal" and params.get("state"):
            where.append("state=?")
            args.append(params["state"])
        w = ("WHERE " + " AND ".join(where)) if where else ""
        return self.store.list_items(kind, w, args)

    def patch_item(self, kind: str, item_id: str, body: dict, *, actor: str) -> dict:
        item = self.store.get_item(kind, item_id)
        if item is None:
            raise Reject(404, "not_found", f"{kind} {item_id} が無い")
        if not isinstance(body, dict):
            raise Reject(422, "schema", "本文が JSON の物でない")
        body = dict(body)
        mutable = {"body"}
        if kind == "promise":
            mutable |= {"todo_id", "due_at"}
        if kind == "schedule":
            mutable |= {"start_at", "end_at"}
        if set(body) - mutable - {"version"}:
            raise Reject(422, "schema", "更新できない項目がある")
        version = body.pop("version", None)
        if type(version) is not int or version < 1:
            raise Reject(422, "version_required", "対象の版が要る（S-9.5）")
        if version != _item_version(item):
            raise Reject(409, "stale_version", "対象の版と一致しない（S-9.5）")
        after = dict(item)
        after.update({k: v for k, v in body.items() if k != "state"})
        if errs := validate_item(kind, {**after, "state": item["state"]}):
            raise Reject(422, "schema", "; ".join(errs[:5]))
        if kind == "schedule" and "start_at" in after and "end_at" in after:
            if timeutil.parse_ts(after["end_at"]) < timeutil.parse_ts(after["start_at"]):
                raise Reject(422, "end_before_start", "予定の終了は開始以降（S-9.4）")
        after["updated_at"] = timeutil.now_ts()
        after["version"] = _item_version(item) + 1
        self.store.put_item(kind, after, actor=actor, kind_of_change="patch", before=item)
        return after

    def transition_item(self, kind: str, item_id: str, body: dict, *, actor: str) -> dict:
        if not body.get("to") or not (body.get("reason") or "").strip():
            raise Reject(422, "reason_required", "遷移には to と reason が要る（S-9.4。理由は必須）")
        item = self.store.get_item(kind, item_id)
        if item is None:
            raise Reject(404, "not_found", f"{kind} {item_id} が無い")
        allowed, _ = self.ITEM_STATES[kind]
        to = body["to"]
        if to not in allowed:
            raise Reject(409, "invalid_transition", f"{to} は {kind} の状態ではない（S-9.4 の表）")
        cur = item["state"]
        initial = self.ITEM_STATES[kind][1][0]
        if to == cur or (cur != initial and to != initial):
            raise Reject(409, "invalid_transition", "表にない遷移（S-9.4）")
        version = body.get("version")
        if type(version) is not int or version < 1:
            raise Reject(422, "version_required", "対象の版が要る（S-9.5）")
        if version != _item_version(item):
            raise Reject(409, "stale_version", "対象の版と一致しない（S-9.5）")
        after = dict(item)
        after["state"] = to
        after["updated_at"] = timeutil.now_ts()
        after["version"] = _item_version(item) + 1
        after["last_transition"] = {"to": to, "reason": body["reason"], "by": actor, "at": timeutil.now_ts()}
        self.store.put_item(kind, after, actor=actor, kind_of_change=f"transition:{cur}->{to}", before=item)
        return after

    # ---- 判断の記録（S-9.6、D-8）----
    def record_decision(self, body: dict, *, actor: str, via: str | None) -> dict:
        d = dict(body)
        d.setdefault("id", ids.new_ulid())
        if not isinstance(d.get("recorded_by"), dict):
            d["recorded_by"] = {"principal": actor}
        # attested は入口が決める。本文の値は無視する（D-8）
        d["attested"] = via == "hq"
        d["ts"] = d.get("ts") or timeutil.now_ts()
        if errs := validate_decision(d):
            raise Reject(422, "schema", "; ".join(errs[:5]))
        if d["basis"] == "user_said" and not (d.get("quote") or "").strip():
            raise Reject(422, "quote_required", "basis が user_said なら quote が要る（S-9.6）")
        if hits := self.secret_scan({"title": d.get("title"), "body": d.get("body"), "reason": d.get("reason"), "quote": d.get("quote")}):
            raise Reject(422, "secret_detected", "自由文に秘密検査に当たる値がある（S-15.3）")
        d["status"] = "active"
        old = None
        if d.get("supersedes"):
            old = self.store.get_decision(d["supersedes"])
            if old is None:
                raise Reject(422, "bad_request", f"supersedes の先 {d['supersedes']} が無い")
        with self.store.transaction():
            self.store.put_decision(d)
            if old is not None:
                self.store.supersede_decision(old["id"])
        self.emit("decision.recorded", d["id"], {
            "decision_id": d["id"], "kind": d["kind"], "project": d.get("project"),
            "supersedes": d.get("supersedes"), "principal": actor, "attested": bool(d["attested"])})
        return d

    def list_decisions(self, params: dict) -> list[dict]:
        where, args = ["status=?"], [params.get("status") or "active"]
        if params.get("project"):
            where.append("project=?")
            args.append(params["project"])
        if params.get("kind"):
            where.append("kind=?")
            args.append(params["kind"])
        if params.get("since"):
            where.append("ts>=?")
            args.append(params["since"])
        return self.store.list_decisions("WHERE " + " AND ".join(where), args)

    def get_decision(self, decision_id: str) -> dict | None:
        d = self.store.get_decision(decision_id)
        if d is None:
            return None
        # 置き換えの履歴を返す（S-9.6）
        chain = [d]
        cur = d
        while cur.get("supersedes"):
            prev = self.store.get_decision(cur["supersedes"])
            if prev is None:
                break
            chain.append(prev)
            cur = prev
        newer = self.store.list_decisions("WHERE supersedes=?", [decision_id])
        return {"decision": d, "chain": chain, "superseded_by": [n["id"] for n in newer]}


def _body_size(body: Any, body_bytes: int | None) -> int:
    if body_bytes is not None:
        return body_bytes
    import json

    return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def _same_content(stored: dict, body: dict) -> bool:
    a = {k: v for k, v in stored.items() if k != "as_of"}
    b = {k: v for k, v in body.items() if k != "as_of"}
    return a == b


def _item_version(item: dict) -> int:
    return int(item.get("version") or 1)

"""通知の配送（S-22.4、S-19.7、T-55、T-81）。

submit(n)       通知を保存し、すぐ 1 回配送を試みる。結果の state を返す。
deliver_due()   送れていない通知（pending で待ちが明けた物、held）を作った順に試す。周期（loop.py）から呼ぶ。

規則:
  - 配送先ごとの枠（policy.rate_per_min）。直近 60 秒に送れた数が枠に達していたら held のまま保持する。捨てない。
    枠が空いたら作った順に再配送する。旧メールの 3 件制限は持たない（T-81）。
  - 送る前に guard で秘密・個人情報を検査する（block なら送らず `notification.failed` を書く）。
  - 失敗は成功として数えない。`notification.failed` を書き、待ちを置いて再配送する（base × 2^回数、最大 max）。
    4xx など再試行しても通らない失敗は上限回数（PERMANENT_MAX）で failed にする。他の通知と処理は止めない。
  - 同じ dedupe_key で pending か held の通知があれば、新しく作らない（重複の抑止）。
"""
from __future__ import annotations

import json
import hashlib
import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Callable, Protocol

from axcommon import ids, timeutil

from . import DEST_NTFY, KIND_QUESTION
from .guard import Guard, Verdict
from .ntfy import Receipt, TEST_MESSAGE, TEST_TITLE
from .policy import DestinationPolicy, default_policies
from .store import NotifyStore, serialized

log = logging.getLogger("hq.notify")

PERMANENT_MAX = 5
SUMMARY_MAX = 200


class Sender(Protocol):
    def send(self, *, title: str, message: str, priority: int = 3, tags: list[str] | None = None, click: str | None = None) -> Receipt: ...


@dataclass
class Notification:
    kind: str
    title: str
    message: str
    todo_id: str | None = None
    qversion: int | None = None
    ref: str | None = None              # 対象を開く参照（HQ の画面の参照。S-22.4）
    priority: int = 3
    tags: tuple = ()
    occurred_at: str | None = None
    dest: str = DEST_NTFY
    dedupe_key: str | None = None
    extra: dict = field(default_factory=dict)


def one_line(text: str, n: int = SUMMARY_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def open_ref(todo_id: str | None) -> str | None:
    """HQ で同じ ToDo を開く参照（S-22.4）。HQ の画面の経路は `#/todo/<ID>`（b10b の画面と合わせる要。OPEN.md の b10a）。"""
    return f"#/todo/{todo_id}" if todo_id else None


class Dispatcher:
    def __init__(self, store: NotifyStore, senders: dict[str, Sender], *, policies: dict[str, DestinationPolicy] | None = None,
                 guard: Guard | None = None, sink=None, now: Callable[[], Any] | None = None, hq_base_url: str | None = None,
                 defaults=None):
        self.store, self.senders = store, senders
        self.policies = policies or default_policies(defaults)
        self.guard = guard or Guard(None)
        self.sink = sink
        self._now = now or timeutil.now
        self.hq_base_url = (hq_base_url or "").rstrip("/") or None
        self.failures = 0

    # --- 事象 ---
    def _emit(self, kind: str, subject: str, data: dict) -> None:
        if self.sink is None:
            return
        try:
            self.sink.emit(kind, subject, data)
        except Exception:  # noqa: BLE001  記録の失敗で配送を止めない（S-2.2）
            log.error("%s を書けない", kind)

    def _click(self, ref: str | None) -> str | None:
        if not ref or not self.hq_base_url:
            return None
        return f"{self.hq_base_url}/{ref}"

    def _inspect(self, row: dict) -> Verdict:
        dest = row["dest"]
        policy = self.policies.get(dest) or DestinationPolicy(dest, 0, on_hit="block")
        verdict = self.guard.inspect(row["title"], row["message"], policy)
        if verdict.action == "block":
            return verdict
        # Metadata cannot be masked without changing its meaning; refuse instead.
        metadata = json.dumps([row.get("tags", []), self._click(row.get("ref")),
                               getattr(self.senders.get(dest), "topic", None)], ensure_ascii=False)
        meta = self.guard.inspect("", metadata, replace(policy, on_hit="block", on_unverifiable="block"))
        return meta if meta.action == "block" else verdict

    # --- 入口 ---
    @serialized
    def submit(self, n: Notification) -> dict:
        now = self._now()
        if n.dedupe_key and self.store.has_open(n.dedupe_key):
            return {"id": None, "state": "duplicate"}
        ts = timeutil.format_ts(now)
        nid = ids.new_ulid()
        verdict = self._inspect({"dest": n.dest, "title": n.title, "message": n.message,
                                 "tags": list(n.tags), "ref": n.ref or open_ref(n.todo_id)})
        blocked = verdict.action == "block"
        self.store.add({"id": nid, "dest": n.dest, "kind": n.kind, "todo_id": n.todo_id, "qversion": n.qversion,
                        "dedupe_key": n.dedupe_key, "title": one_line(verdict.title, 120), "message": one_line(verdict.message),
                        "ref": None if blocked else n.ref or open_ref(n.todo_id), "priority": n.priority,
                        "tags": [] if blocked else list(n.tags), "state": "blocked" if blocked else "pending",
                        "attempts": 0, "next_try_at": None, "created_at": ts, "occurred_at": n.occurred_at or ts})
        if blocked:
            self.store.update(nid, last_error=f"guard:{verdict.reason}")
            self._emit("notification.failed", n.todo_id or nid,
                       {"notification_id": nid, "todo_id": n.todo_id, "dest": n.dest, "kind": n.kind,
                        "result": "blocked", "reason": f"guard_{verdict.reason}", "categories": verdict.categories})
            return {"id": nid, "state": "blocked"}
        self._deliver_dest(n.dest, now)   # 古い保持分が先（作った順を守る）
        return {"id": nid, "state": self.store.get(nid)["state"]}

    def receipt_lookup(self, delivery_id: str, destination: str) -> dict:
        row = self.store.receipt_get(delivery_id)
        result = {"delivery_id": delivery_id, "destination": destination, "status": "unknown",
                  "accepted": False, "accepted_at": None}
        if row is None:
            return result
        if row["destination"] != destination:
            return {**result, "status": "conflict"}
        result["status"] = row["state"]
        if row["accepted_at"] is not None and isinstance(row["provider_id"], str) and row["provider_id"]:
            result["accepted"] = True
            result["accepted_at"] = row["accepted_at"]
        if row["state"] == "delivered":
            result["delivered_at"] = row["delivered_at"]
        elif row["state"] == "retry":
            result["next_retry"] = row["next_retry"]
        return result

    @serialized
    def receipt_send(self, delivery_id: str, destination: str, body: dict, *, collect_only: bool = False) -> dict:
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        if collect_only:
            existing = self.store.receipt_get(delivery_id)
            if existing and (existing["destination"] != destination or existing["payload_hash"] != digest):
                return {"delivery_id": delivery_id, "destination": destination, "status": "conflict",
                        "accepted": False, "accepted_at": None}
            result = self.receipt_lookup(delivery_id, destination)
            return {**result, "accepted": False, "accepted_at": None}
        now = self._now()
        row, claimed = self.store.receipt_claim(delivery_id, destination, digest, now.timestamp())
        if row["destination"] != destination or row["payload_hash"] != digest:
            return {"delivery_id": delivery_id, "destination": destination, "status": "conflict",
                        "accepted": False, "accepted_at": None}
        if not claimed:
            return self.receipt_lookup(delivery_id, destination)
        sender, policy = self.senders.get(DEST_NTFY), self.policies.get(DEST_NTFY)
        if sender is None or policy is None:
            self.store.receipt_update(delivery_id, "retry", next_retry=now.timestamp() + 60)
            return self.receipt_lookup(delivery_id, destination)
        since = timeutil.format_ts(now - timedelta(seconds=60))
        if self.store.sent_since(DEST_NTFY, since) >= policy.rate_per_min:
            self.store.receipt_update(delivery_id, "retry", next_retry=now.timestamp() + 60)
            return self.receipt_lookup(delivery_id, destination)
        if len(canonical.encode("utf-8")) > 3500:
            self.store.receipt_update(delivery_id, "blocked")
            return self.receipt_lookup(delivery_id, destination)
        verdict = self._inspect({"dest": DEST_NTFY, "title": "AX change notification", "message": canonical, "tags": [], "ref": None})
        if verdict.action == "block":
            self.store.receipt_update(delivery_id, "blocked")
            return self.receipt_lookup(delivery_id, destination)
        try:
            receipt = sender.send(title=one_line(verdict.title, 120), message=verdict.message, priority=3, tags=[], click=None)
        except Exception:
            return self.receipt_lookup(delivery_id, destination)
        if receipt.ok and isinstance(receipt.id, str) and receipt.id:
            self.store.receipt_accepted(delivery_id, self._now().timestamp(), receipt.id)
        return self.receipt_lookup(delivery_id, destination)

    def _deliver_dest(self, dest: str, now) -> dict:
        out: dict[str, int] = {}
        for row in self.store.deliverable(dest, timeutil.format_ts(now)):
            st = self._attempt(row, now)
            out[st] = out.get(st, 0) + 1
        return out

    @serialized
    def deliver_due(self) -> dict:
        now = self._now()
        out: dict[str, int] = {"sent": 0, "held": 0, "pending": 0, "blocked": 0, "failed": 0}
        for dest in sorted(self.policies):
            for st, n in self._deliver_dest(dest, now).items():
                out[st] = out.get(st, 0) + n
        return out

    # --- 1 件 ---
    def _attempt(self, row: dict, now) -> str:
        dest = row["dest"]
        policy = self.policies.get(dest)
        sender = self.senders.get(dest)
        if policy is None or sender is None:
            return self._fail(row, now, "no_sender", transient=True, policy=policy)
        since = timeutil.format_ts(now - timedelta(seconds=60))
        if self.store.sent_since(dest, since) >= policy.rate_per_min:
            if row["state"] != "held":
                self.store.update(row["id"], state="held")
            return "held"
        verdict = self._inspect(row)
        if verdict.action == "block":
            self.store.update(row["id"], state="blocked", last_error=f"guard:{verdict.reason}")
            self._emit("notification.failed", row["todo_id"] or row["id"],
                       {"notification_id": row["id"], "todo_id": row["todo_id"], "dest": dest, "kind": row["kind"],
                        "result": "blocked", "reason": f"guard_{verdict.reason}", "categories": verdict.categories})
            return "blocked"
        try:
            receipt = sender.send(title=verdict.title, message=verdict.message, priority=row["priority"], tags=row["tags"],
                                  click=self._click(row["ref"]))
        except Exception as exc:  # One destination must not stop the remaining queue.
            return self._fail(row, now, f"sender:{type(exc).__name__}", transient=True, policy=policy)
        if not receipt.ok:
            return self._fail(row, now, receipt.reason or "send_failed", transient=receipt.transient, policy=policy)
        ts = timeutil.format_ts(now)
        self.store.update(row["id"], state="sent", sent_at=ts, external_id=receipt.id, last_error=None, next_try_at=None)
        if row["kind"] == KIND_QUESTION and row["todo_id"] and row["qversion"] is not None:
            self.store.question_success(row["todo_id"], int(row["qversion"]), ts)
            self._emit("question.notified", row["todo_id"],
                       {"todo_id": row["todo_id"], "version": row["qversion"], "notification_id": row["id"], "dest": dest,
                        "external_id": receipt.id, "at": ts, "result": "sent"})
        return "sent"

    def _fail(self, row: dict, now, reason: str, *, transient: bool, policy: DestinationPolicy | None) -> str:
        attempts = int(row["attempts"]) + 1
        self.failures += 1
        wait = (policy or next(iter(default_policies().values()))).retry_wait_s(attempts)
        giving_up = (not transient) and attempts >= PERMANENT_MAX
        nxt = None if giving_up else timeutil.format_ts(now + timedelta(seconds=wait))
        self.store.update(row["id"], state="failed" if giving_up else "pending", attempts=attempts, next_try_at=nxt, last_error=reason)
        self._emit("notification.failed", row["todo_id"] or row["id"],
                   {"notification_id": row["id"], "todo_id": row["todo_id"], "dest": row["dest"], "kind": row["kind"],
                    "result": "failed" if giving_up else "retry", "reason": reason, "attempts": attempts, "next_retry_at": nxt})
        return "failed" if giving_up else "pending"

    @serialized
    def send_test_once(self) -> dict:
        previous = self.store.digest_get("test-notification")
        if previous is not None and previous["state"] == "sent":
            return {"ok": False, "status": 409, "reason": "already_sent"}
        result = self.send_test()
        if result.get("ok"):
            self.store.digest_put("test-notification", result.get("id"), "sent", timeutil.format_ts(self._now()))
        return {**result, "status": 200 if result.get("ok") else 502}

    # --- テスト通知（S-24 条件 4、T-55） ---
    def send_test(self, *, dest: str = DEST_NTFY) -> dict:
        """件名「AX テスト通知」の 1 件。受け付けられたか（HTTP 200 と ID）だけを返す。繰り返さない（呼ぶ側が 1 回だけ呼ぶ）。"""
        sender = self.senders.get(dest)
        if sender is None or not hasattr(sender, "send_test"):
            return {"ok": False, "reason": "no_sender"}
        verdict = self._inspect({"dest": dest, "title": TEST_TITLE, "message": TEST_MESSAGE, "tags": []})
        if verdict.action != "send":
            self._emit("notification.failed", "test", {"kind": "test", "dest": dest, "reason": f"guard_{verdict.reason}"})
            return {"ok": False, "reason": f"guard_{verdict.reason}"}
        try:
            receipt = sender.send_test()
        except Exception as exc:
            receipt = Receipt(False, reason=f"sender:{type(exc).__name__}")
        ts = timeutil.format_ts(self._now())
        if not receipt.ok:
            self._emit("notification.failed", "test", {"notification_id": None, "todo_id": None, "dest": dest, "kind": "test",
                                                        "result": "failed", "reason": receipt.reason, "at": ts})
        return {"ok": receipt.ok, "id": receipt.id, "reason": receipt.reason, "at": ts}

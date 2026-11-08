"""許された操作の実行（S-10.3）。検査（policy.py）を通った物だけを API で実行する。

- 失敗は 1 件ずつ記録して先へ進む（1 件の失敗で周回を止めない）。4xx は ok=False（code つき）、届かないとき（Unavailable）は
  ok=False, unavailable=True。台帳への書き込みが届かないときは自分のボリュームの outbox/ に溜め、戻ったら順に送る（S-2.2）。
- control.issue は reason を命令に入れる。on_expiry の既定は ask_issuer（exclude_resource は release）。
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import math
import re
from pathlib import Path

from axcommon import ids, timeutil
from axcommon.client import ApiError, Unavailable
from axcommon.events import Outbox

LEDGER_OUTBOX = "ledger"


class Executor:
    def __init__(self, clients, outbox_dir: str | Path, *, parked_eject_days: float = 1.0, host: str = "unknown"):
        self.c = clients
        self.outbox = Outbox(outbox_dir)
        self.memory_outbox = Outbox(Path(outbox_dir) / "memory")
        self.parked_eject_days = parked_eject_days
        self.host = host

    # ---- 台帳への書き込み（届かなければ溜める）----
    def ledger_create(self, todo: dict) -> dict:
        todo = dict(todo)
        todo.setdefault("id", ids.new_ulid())
        try:
            r = self.c.ledger.create_todo(todo)
            return {"ok": True, "todo_id": (r or {}).get("id", todo["id"])}
        except Unavailable:
            self.outbox.append(LEDGER_OUTBOX, "/v1/todos", "POST", todo, entry_id=ids.new_ulid())
            return {"ok": True, "queued": True, "todo_id": todo["id"]}
        except ApiError as e:
            return {"ok": False, "code": e.code, "status": e.status}

    def flush_ledger(self) -> int:
        def _send(entry: dict) -> None:
            self.c.ledger.request(entry["method"], entry["path"], json_body=entry["body"])

        return self.outbox.drain(_send)

    def remember(self, body: dict) -> str:
        if self.c.memory is None:
            return "skipped"
        try:
            self.c.memory.remember(body)
            return "ok"
        except Unavailable:
            self.memory_outbox.append("memory-connector", "/v1/remember", "POST", body)
            return "queued"
        except ApiError as e:
            return f"failed:{e.code}"

    def flush_memory(self) -> int:
        if self.c.memory is None:
            return 0
        return self.memory_outbox.drain(lambda entry: self.c.memory.remember(entry["body"]))

    def eject_event(self, ev: dict, reason: str, *, check_at: str | None = None) -> dict:
        """事象を台帳へ出す（ledger.eject。外へ出す = parked の ToDo。S-9.2）。"""
        when = check_at or timeutil.format_ts(timeutil.now() + timedelta(days=self.parked_eject_days))
        src = ev.get("source") if isinstance(ev.get("source"), dict) else {}
        todo = {"title": f"管理役が扱えなかった: {ev.get('kind')} {ev.get('subject', '')}"[:200], "state": "parked", "owner": "system",
                "reason": reason, "check_at": when, "source": {"event_id": ev.get("id")}}
        if ev.get("project"):
            todo["project"] = ev["project"]
        if ev.get("subloop"):
            todo["subloop"] = ev["subloop"]
        if src.get("run_id"):
            todo["source"]["run_id"] = src["run_id"]
        return self.ledger_create(todo)

    # ---- 操作 ----
    def run(self, action: dict, *, event_ids: list[str] | None = None) -> dict:
        fn = getattr(self, "_" + action["type"].replace(".", "_"))
        try:
            res = fn(action)
        except Unavailable:
            res = {"ok": False, "unavailable": True}
        except ApiError as e:
            res = {"ok": False, "code": e.code, "status": e.status}
        return {"type": action["type"], **res}

    def _control_issue(self, a: dict) -> dict:
        cmd = dict(a["command"])
        cmd["reason"] = cmd.get("reason") or a["reason"]
        cmd.setdefault("on_expiry", "release" if cmd.get("op") == "exclude_resource" else "ask_issuer")
        cmd["issued_by"] = "manager"
        r = self.c.control.issue(cmd)
        return {"ok": True, "command_id": (r or {}).get("id")}

    def _control_release(self, a: dict) -> dict:
        self.c.control.release(a["command_id"], a["reason"])
        return {"ok": True, "command_id": a["command_id"]}

    def _control_renew(self, a: dict) -> dict:
        self.c.control.renew(a["command_id"], a["expires_at"], a["reason"])
        return {"ok": True, "command_id": a["command_id"]}

    def _ledger_eject(self, a: dict) -> dict:
        todo = {"title": f"管理役が台帳へ出した: {a['subject']}"[:200], "state": "parked", "owner": "system", "reason": a["reason"],
                "check_at": a["check_at"], "source": {"event_id": a["subject"]} if len(a["subject"]) == 26 else {}}
        if not todo["source"]:
            todo.pop("source")
        return self.ledger_create(todo)

    def _ledger_add(self, a: dict) -> dict:
        todo = dict(a["todo"])
        todo.setdefault("owner", "system")
        todo.setdefault("state", "todo")
        todo.setdefault("reason", a["reason"])
        return self.ledger_create(todo)

    def _timer_create(self, a: dict) -> dict:
        timer = dict(a["timer"])
        timer.setdefault("reason", a["reason"])
        r = self.c.control.create_timer(timer)
        return {"ok": True, "timer_id": (r or {}).get("id")}

    def _noop(self, a: dict) -> dict:
        return {"ok": True}


def propose_rejections(executor, ctx: dict, actions: list[dict], rejected: list[dict], results: list[dict]) -> None:
    """拒否された設定変更だけを、値や理由を複写せず利用者へ提案する。"""
    failures = [(r["index"], r.get("code")) for r in rejected]
    failures += [(r["index"], r.get("code")) for r in results if not r.get("ok")]
    for index, code in failures:
        action = actions[index]
        cmd = action.get("command") or {}
        args = cmd.get("args") or {}
        target, name = cmd.get("target"), args.get("name")
        if action.get("type") != "control.issue" or cmd.get("op") != "set_knob":
            continue
        if code not in {"knob_not_manager", "limit_change_forbidden", "who_not_allowed", "user_set_knob"}:
            continue
        if not all(isinstance(x, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", x) for x in (target, name)):
            continue
        plugin = ctx.get("plugins", {}).get(target, {})
        spec = (plugin.get("manifest", {}).get("knobs") or {}).get(name)
        if not isinstance(spec, dict):
            continue
        marked = bool((plugin.get("user_set_knobs") or {}).get(name)) or code == "user_set_knob"
        model_setting = target == "worker-pool" and name in {
            "role_candidates", "family_cap", "model_cap", "jev_enabled", "lower_fallback_enabled",
            "lower_fallback_wait_s", "promotion_passes", "demotion_failures", "free_success_threshold",
            "free_minimum_runs", "free_recent_failure_min", "retired",
        }
        if not (marked or model_setting):
            continue
        # 固定 ID で、通信断後の再送や同じ提案の同時作成を台帳側で重複排除する。
        key = json.dumps([target, name, args.get("value")], sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        digest = int.from_bytes(hashlib.sha256(key.encode()).digest()[:16], "big") >> 2
        alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        todo_id = ""
        for _ in range(26):
            todo_id = alphabet[digest & 31] + todo_id
            digest >>= 5
        value = args.get("value")
        safe_numeric_names = {"family_cap", "model_cap", "jev_enabled", "lower_fallback_enabled",
                              "lower_fallback_wait_s", "promotion_passes", "demotion_failures",
                              "free_success_threshold", "free_minimum_runs", "free_recent_failure_min",
                              "every_s", "max_parallel_items"}
        is_boolean = spec.get("type") == "boolean" and isinstance(value, bool)
        is_number = (spec.get("type") in ("integer", "number") and type(value) in (int, float)
                     and math.isfinite(value) and spec.get("min", -1000000) <= value <= spec.get("max", 1000000))
        shown = name in safe_numeric_names and (is_boolean or is_number)
        detail = f"提案値: {json.dumps(value)}。" if shown else "提案値は安全のため表示していません。"
        todo = {"id": todo_id, "title": "設定変更の提案", "state": "waiting_user", "owner": "user",
                "reason": "管理役には設定の変更権限がありません",
                "card": {"question": f"{target} の {name} の設定変更を検討しますか。{detail}",
                         "waiting_reason": "preference"}}
        executor.ledger_create(todo)

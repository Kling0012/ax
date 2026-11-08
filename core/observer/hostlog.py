"""ホスト見張りのログ `/state/hostwatch.log` の写し（D-10.3 の 6）と、sudo の不調のカード（S-11.6）。

ログの 1 行は S-3.2 の形の事象（JSON）。source.component は hostwatch。ホスト見張りは時系列記録に届かないので、ファイルに書く。
オブザーバーが戻ったら、この写しで時系列記録へ送る（同じ id は無視されるので重複しない）。位置は自分のボリュームの hostlog.cursor（inode と offset）。
`host.sudo_unavailable` を読んだら、利用者向けのカードを 1 件だけ台帳に作る（同じ原因で重ねない）。`host.sudo_restored` で「不調ではない」に戻す。
`deploy.rolled_back`（中核が healthz を通らず前のタグへ戻した。F-18.3 の (f)）を読んだら、ToDo（parked）を台帳に作る。台帳に届かないときは自分のボリュームに溜めて、次の周期にやり直す。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from axcommon import schemas
from axcommon import timeutil
from axcommon.client import ApiError, LedgerClient, TimelineClient, Unavailable
from axcommon.events import EventSink

from .sources import json_load, json_save

LOG_NAME = "hostwatch.log"
MAX_LINE = 16 * 1024
CARD_TITLE = "ホストの sudo -n が使えない（AX の特権操作が止まっている）"


class HostLogSync:
    def __init__(self, state_dir: str | Path, data_dir: str | Path, timeline: TimelineClient, sink: EventSink,
                 ledger: LedgerClient | None):
        self.path = Path(state_dir) / LOG_NAME
        self.cursor = Path(data_dir) / "hostlog.cursor"
        self.sudo_state = Path(data_dir) / "sudo_card.json"
        self.todo_queue = Path(data_dir) / "host_todos.json"
        self.effects = Path(data_dir) / "host_effects.json"
        self.timeline, self.sink, self.ledger = timeline, sink, ledger
        self.last = {"posted": 0, "invalid": 0, "deferred": False}

    def run(self) -> dict:
        stats = {"posted": 0, "invalid": 0, "deferred": False}
        self.last = stats
        try:
            st = os.stat(self.path)
        except OSError:
            self._card()
            return stats
        cur = json_load(self.cursor, {})
        offset = cur.get("offset", 0) if cur.get("inode") == st.st_ino else 0
        if not isinstance(offset, int) or offset < 0 or offset > st.st_size:
            offset = 0
        try:
            with open(self.path, "rb") as f:
                f.seek(offset)
                blob = f.read()
        except OSError:
            return stats
        pos = offset
        while True:
            nl = blob.find(b"\n")
            if nl < 0:
                break  # 書きかけの行は次の周期に回す
            raw, blob = blob[:nl], blob[nl + 1:]
            step = nl + 1
            if raw.strip():
                ev = self._parse(raw)
                if ev is None:
                    stats["invalid"] += 1
                else:
                    self._note_sudo(ev)
                    try:
                        self.timeline.post_events(ev)
                        stats["posted"] += 1
                    except Unavailable:
                        stats["deferred"] = True
                        break
                    except ApiError:
                        stats["invalid"] += 1
            pos += step
        json_save(self.cursor, {"inode": st.st_ino, "offset": pos})
        self._card()
        return stats

    @staticmethod
    def _parse(raw: bytes) -> dict | None:
        if len(raw) > MAX_LINE:
            return None
        try:
            ev = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(ev, dict) or schemas.errors("event", ev) or ev["source"]["component"] != "hostwatch":
            return None
        return ev

    def _note_sudo(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind not in ("deploy.rolled_back", "host.sudo_unavailable", "host.sudo_restored"):
            return
        seen = json_load(self.effects, {})
        if ev["id"] in seen:
            return
        if kind == "deploy.rolled_back":
            self._queue_rollback_todo(ev)
        else:
            st = json_load(self.sudo_state, {})
            if kind == "host.sudo_unavailable":
                if not st.get("unavailable"):
                    st = {"unavailable": True, "card": False, "event_id": ev["id"]}
            else:
                st = {"unavailable": False, "card": False}
            if not json_save(self.sudo_state, st):
                raise OSError("sudo state was not saved")
        seen[ev["id"]] = kind
        if not json_save(self.effects, seen):
            raise OSError("host log effects were not saved")

    def _queue_rollback_todo(self, ev: dict) -> None:
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        svc = str(data.get("service") or ev.get("subject") or "?")[:60]
        q = json_load(self.todo_queue, [])
        if any(t.get("key") == ev.get("id") for t in q):
            return
        q.append({"key": ev.get("id"), "todo": {
            "id": ev["id"], "source": {"event_id": ev["id"]},
            "title": f"配備を前のタグへ戻した: {svc}", "state": "parked", "owner": "system",
            "reason": f"新しいタグ {str(data.get('from_tag'))[:12]} で {svc} の healthz が通らず、前のタグ {str(data.get('to_tag'))[:12]} で起動し直した（deploy.rolled_back）。新しいイメージは残してある。原因を直して再度 deploy する",
            "check_at": timeutil.add_seconds(timeutil.now_ts(), 86400)}})
        if not json_save(self.todo_queue, q):
            raise OSError("rollback ToDo was not saved")

    def _flush_todos(self) -> None:
        q = json_load(self.todo_queue, [])
        if not q or self.ledger is None:
            return
        rest = []
        for item in q:
            try:
                self.ledger.create_todo(item["todo"])
            except Unavailable:
                rest.append(item)
            except ApiError as e:
                if e.status != 409 or e.code != "already_exists":
                    rest.append(item)
        if len(rest) != len(q):
            json_save(self.todo_queue, rest)

    def _card(self) -> None:
        """sudo が使えない間、カードを 1 件だけ作る。台帳が落ちていれば次の周期にやり直す。"""
        self._flush_todos()
        st = json_load(self.sudo_state, {})
        if not st.get("unavailable") or st.get("card") or self.ledger is None:
            return
        todo = {
            "id": st["event_id"], "source": {"event_id": st["event_id"]},
            "title": CARD_TITLE, "state": "waiting_user", "owner": "user",
            "card": {"question": "ホスト見張りの sudo -n が失敗している。パスワードなしの sudo を戻せるか確かめてほしい。戻れば自動で再開する。",
                     "options": ["確かめた", "しばらく待つ"], "waiting_reason": "physical"},
        }
        try:
            self.ledger.create_todo(todo)
        except Unavailable:
            return
        except ApiError as e:
            if e.status != 409 or e.code != "already_exists":
                return
        st["card"] = True
        json_save(self.sudo_state, st)

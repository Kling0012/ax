"""事象の組み立てと送信、送れないときの outbox（D-2.2 axcommon.events、S-2.2、S-3.2）。

- build_event(): S-3.2 の形の事象を作る（ID は ULID、時刻は UTC ミリ秒）。schemas の event で検査する。
- EventSink.emit(): 作って送る。送れなければ outbox/ に溜め、**例外を出さない**（届かないことを理由に自分の処理を止めない。S-2.2）。
- EventSink.flush(): 溜めた物を溜めた順に送る。最初の失敗で止める（順序を保つ）。送れた物は sent/ の日付ファイルに移す（削除しない）。
- 同じ id を 2 回送ってもよい（時系列記録が無視して成功を返す。S-3.5）。そのため、送信の成功を確かめる前に落ちても二重にならない。
outbox は 1 行 1 JSON（schemas/outbox-entry.schema.json）の `pending.jsonl`。書き込みは fcntl.flock で排他する。
秘密: 呼び出し側が set_redactor() で S-15.3 の検査（axcommon.redact）を渡すと、data に通してから溜める。時系列記録も書き込み時に検査する（二重の防御）。
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from . import ids, timeutil
from .client import ApiError, TimelineClient, Unavailable

Redactor = Callable[[Any], Any]
_redactor: Redactor | None = None


def set_redactor(fn: Redactor | None) -> None:
    global _redactor
    _redactor = fn


def build_event(*, component: str, host: str, kind: str, subject: str, data: dict | None = None, project: str | None = None,
                subloop: str | None = None, run_id: str | None = None, node: str | None = None, corr: str | None = None,
                cause: str | None = None, source_extra: dict | None = None, event_id: str | None = None, validate: bool = True) -> dict:
    """S-3.2 の事象。source_extra は principal、session、via（入口が確かめた値だけ。S-3.2）。"""
    source: dict[str, Any] = {"component": component, "host": host}
    if run_id is not None:
        source["run_id"] = run_id
    if node is not None:
        source["node"] = node
    if source_extra:
        source.update(source_extra)
    d = data or {}
    if _redactor is not None:
        d = _redactor(d)
    ev = {
        "id": event_id or ids.new_ulid(),
        "ts": timeutil.now_ts(),
        "source": source,
        "project": project,
        "subloop": subloop,
        "kind": kind,
        "subject": subject,
        "data": d,
        "corr": corr,
        "cause": cause,
    }
    if validate:
        from . import schemas

        schemas.validate("event", ev)
    return ev


class Outbox:
    """送れなかった要求の待ち行列（ボリュームの outbox/）。"""

    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)
        self.pending = self.dir / "pending.jsonl"
        self.sent_dir = self.dir / "sent"
        self._lock = threading.Lock()

    def _flock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        f = open(self.dir / ".lock", "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def append(self, target: str, path: str, method: str, body: Any, entry_id: str | None = None) -> dict:
        entry = {"target": target, "path": path, "method": method, "body": body, "queued_at": timeutil.now_ts(), "attempts": 0,
                 "id": entry_id or ids.new_ulid()}
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            lk = self._flock()
            try:
                with open(self.pending, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                lk.close()
        return entry

    def read_all(self) -> list[dict]:
        try:
            text = self.pending.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # 壊れた行は飛ばす（落ちた時の書きかけ。fail-soft）
        return out

    def __len__(self) -> int:
        return len(self.read_all())

    def drain(self, send: Callable[[dict], None]) -> int:
        """溜めた順に send(entry) する。例外（Unavailable）で止め、残りを保つ。送れた件数を返す。"""
        with self._lock:
            lk = self._flock()
            try:
                entries = self.read_all()
                sent: list[dict] = []
                for e in entries:
                    try:
                        send(e)
                    except Unavailable:
                        break
                    except ApiError:
                        # 受け取り側が恒久的に拒否した（4xx）。順序を詰まらせないため、rejected/ へ移して先へ進む（削除しない）
                        self._move("rejected", e)
                        sent.append(e)
                        continue
                    self._move("sent", e)
                    sent.append(e)
                if sent:
                    ids_done = {e["id"] for e in sent}
                    rest = [e for e in entries if e["id"] not in ids_done]
                    tmp = self.pending.with_suffix(".tmp")
                    tmp.write_text("".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in rest), encoding="utf-8")
                    os.replace(tmp, self.pending)
                return len(sent)
            finally:
                lk.close()

    def _move(self, kind: str, entry: dict) -> None:
        d = self.dir / kind
        d.mkdir(parents=True, exist_ok=True)
        day = timeutil.now_ts()[:10].replace("-", "")
        with open(d / f"{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


class EventSink:
    """事象を作って時系列記録へ送る。送れなければ outbox へ。"""

    def __init__(self, client: TimelineClient | None, component: str, host: str, outbox_dir: str | os.PathLike, *,
                 project: str | None = None, subloop: str | None = None):
        self.client = client
        self.component, self.host = component, host
        self.project, self.subloop = project, subloop
        self.outbox = Outbox(outbox_dir)
        self.queued_total = 0  # 溜めた回数（healthz の補助用）

    def emit(self, kind: str, subject: str, data: dict | None = None, **kw) -> dict:
        kw.setdefault("project", self.project)
        kw.setdefault("subloop", self.subloop)
        ev = build_event(component=self.component, host=self.host, kind=kind, subject=subject, data=data, **kw)
        self.send(ev)
        return ev

    def send(self, ev: dict) -> bool:
        """True=送れた / False=溜めた。例外を出さない。溜まりがあれば順序を保つため、先に溜まりを流してから送る。"""
        try:
            if self.client is not None:
                if len(self.outbox):
                    self.flush()
                if len(self.outbox) == 0:
                    self.client.post_events(ev)
                    return True
        except (Unavailable, ApiError):
            pass
        self.outbox.append("timeline", "/v1/events", "POST", ev, entry_id=ev["id"])
        self.queued_total += 1
        return False

    def flush(self) -> int:
        if self.client is None:
            return 0

        def _send(entry: dict) -> None:
            self.client.request(entry["method"], entry["path"], json_body=entry["body"])

        return self.outbox.drain(_send)

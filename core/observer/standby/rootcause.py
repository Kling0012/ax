"""根本原因の ToDo（S-11.3 の 3、S-11.4、D-10.2）と、台帳に届かないときの溜め場。

- 作る ToDo は S-9.1 の形。`owner: system`、`subloop: core`、状態 `todo`。`reason` の印は __init__.py。
- 台帳が落ちている（Unavailable。5xx を含む）間は `pending-todos.jsonl` に 1 行 1 件で溜め、戻ったら溜めた順に作る（D-10.2）。
  4xx で拒否された ToDo は `rejected-todos.jsonl` へ移す（作り直しても通らない。削除しない）。
- 同じ溜めの二重作成を避けるため、各 ToDo は `id` を ULID で先に決めておく（台帳が同じ id を 409 で返したら作成済みとみなす）。
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from axcommon import ids, timeutil
from axcommon.client import ApiError, LedgerClient, Unavailable

from . import REASON_CARD, REASON_ROOT, REASON_TOP, TITLE_PREFIX


def _short(text: str, n: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def root_cause_todo(*, check: str, target: str, activation_event: str | None = None, top: bool = False) -> dict:
    """根本原因の ToDo。題には検査名と対象の名前だけを入れる（値や秘密を入れない）。詳しい経緯は `manager.cycle` の事象に残す。"""
    title = f"{TITLE_PREFIX}{check}（{target}）"
    if top:
        title = f"{TITLE_PREFIX}頻発。最優先で根本から直す: {check}（{target}）"
    todo = {
        "id": ids.new_ulid(),
        "title": _short(title, 160),
        "state": "todo",
        "owner": "system",
        "subloop": "core",
        "reason": REASON_TOP if top else REASON_ROOT,
        "check_at": None,
        "artifacts": [],
        "goal_ids": [],
        "promise_ids": [],
        "source": {"event_id": activation_event} if activation_event else {},
    }
    return todo


def frequent_card(*, count: int, window_days: float, limit: int) -> dict:
    """利用者向けのカード（R-O4）。質問ではない知らせなので `owner: user` の ToDo にする（OPEN.md の b10a）。"""
    return {
        "id": ids.new_ulid(),
        "title": _short(f"予備役が{window_days:g}日で{count}回動いた（上限{limit}回）。管理役の根本の修繕を最優先にしている", 160),
        "state": "todo",
        "owner": "user",
        "subloop": "core",
        "reason": REASON_CARD,
        "artifacts": [],
        "goal_ids": [],
        "promise_ids": [],
        "source": {},
    }


class TodoSender:
    """台帳へ ToDo を作る。届かなければ溜める。"""

    def __init__(self, ledger: LedgerClient | None, directory: str | os.PathLike):
        self.ledger = ledger
        self.dir = Path(directory)
        self.pending = self.dir / "pending-todos.jsonl"
        self.rejected = self.dir / "rejected-todos.jsonl"
        self.created: list[str] = []   # この起動で作れた ToDo の id（healthz の補助と試験用）

    # --- 溜め場 ---
    def _lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        f = open(self.dir / ".lock", "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def _append(self, path: Path, todo: dict) -> None:
        lk = self._lock()
        try:
            self._append_locked(path, todo)
        finally:
            lk.close()

    def _append_locked(self, path: Path, todo: dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"queued_at": timeutil.now_ts(), "todo": todo}, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_pending(self) -> list[dict]:
        try:
            lines = self.pending.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # 落ちた時の書きかけは飛ばす（fail-soft）
            if isinstance(row, dict) and isinstance(row.get("todo"), dict):
                out.append(row)
        return out

    def pending_count(self) -> int:
        return len(self.read_pending())

    # --- 作る ---
    def _post(self, todo: dict) -> str:
        """'created' | 'exists' | 'rejected'。Unavailable は上へ出す。"""
        if self.ledger is None:
            raise Unavailable("no ledger")
        try:
            self.ledger.create_todo(todo)
        except ApiError as e:
            if e.status == 409:
                return "exists"
            return "rejected"
        return "created"

    def send(self, todo: dict) -> str:
        """'created' | 'exists' | 'queued' | 'rejected'。例外を出さない。"""
        try:
            res = self._post(todo)
        except Unavailable:
            self._append(self.pending, todo)
            return "queued"
        if res == "rejected":
            self._append(self.rejected, todo)
        else:
            self.created.append(todo["id"])
        return res

    def drain(self) -> int:
        """溜めた物を溜めた順に作る。最初の Unavailable で止める（順序を保つ）。作れた件数を返す。"""
        lk = self._lock()
        try:
            rows = self.read_pending()
            done = 0
            for row in rows:
                try:
                    res = self._post(row["todo"])
                except Unavailable:
                    break
                if res == "rejected":
                    self._append_locked(self.rejected, row["todo"])
                else:
                    self.created.append(row["todo"]["id"])
                done += 1
            if done:
                tmp = self.pending.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows[done:]))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.pending)
            return done
        finally:
            lk.close()

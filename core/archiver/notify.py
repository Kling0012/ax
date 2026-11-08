"""失敗のカード（ToDo）。同じ原因は 1 件（ledger.note_failure の first が真のときだけ呼ばれる）。台帳に届かなければ作らない（fail-soft）。"""
from __future__ import annotations

import logging

from axcommon.client import ApiError, LedgerClient, Unavailable

log = logging.getLogger("archiver")

TITLES = {
    "unreachable": "退避先に届かない",
    "key_rejected": "退避用の鍵が拒否された",
    "dest_full": "退避先の空きが足りない",
    "dest_permission": "退避先に書けない（権限）",
    "verify_mismatch": "退避先の内容が元と一致しない",
    "secrets_dir_refused": "退避元が秘密置き場に入っている",
    "timeout": "退避の転送が時間切れ",
}


class LedgerNotifier:
    def __init__(self, client: LedgerClient):
        self.client = client

    def failure(self, source: str, reason: str, detail: str, *, first: bool, event_id: str | None) -> str | None:
        todo = {"title": f"退避が保留: {source}（{TITLES.get(reason, reason)}）", "state": "todo", "owner": "system", "subloop": "core",
                "reason": f"archive:{source}:{reason}"}
        if event_id:
            todo["source"] = {"event_id": event_id}
        try:
            res = self.client.create_todo(todo)
            return "created:" + str((res or {}).get("id", "")) if isinstance(res, dict) else "created"
        except (Unavailable, ApiError):
            log.warning("カードを作れない（台帳に届かない）")
            return "failed"

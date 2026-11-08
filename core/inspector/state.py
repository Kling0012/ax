"""点検係の状態（inspector-data/state.json。D-2.1「点検の予定と結果」）。書き換えは一時ファイルへ書いて置き換える。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from axcommon import timeutil

HISTORY_MAX = 50


class StateStore:
    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)
        self.path = self.dir / "state.json"
        self.history_path = self.dir / "history.jsonl"

    def load(self) -> dict | None:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        from core.manager.extschemas import errors

        return doc if not errors("inspection-state", doc) else None

    def save(self, state: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def append_history(self, result: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": timeutil.now_ts(), **result}, ensure_ascii=False) + "\n")

    def history(self, n: int = 20) -> list[dict]:
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(x) for x in lines[-n:] if x.strip()]

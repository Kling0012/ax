"""警報の重複除去（S-11.2 の末尾、D-10.1）。(check, target) の組で最後に出した時刻を持つ。ボリュームに JSON で残す。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from axcommon import timeutil

KEEP_FACTOR = 8  # 窓の何倍を過ぎた記録を捨てるか（表を太らせない）


class DedupTable:
    def __init__(self, path: str | os.PathLike, window_min: float):
        self.path = Path(path)
        self.window_s = float(window_min) * 60
        self._t: dict[str, str] = {}
        self._load()

    @staticmethod
    def _k(check: str, target: str) -> str:
        return f"{check}\x1f{target}"

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._t = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            self._t = {}  # 壊れた・無い表は空から（重複して出ることはあっても、止まらない）

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._t, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # 保存できなくてもメモリの表で動く

    def allow(self, check: str, target: str, now: datetime | None = None) -> bool:
        """窓の中で最後に出していなければ True を返し、出した時刻を記録する。"""
        now = now or timeutil.now()
        k = self._k(check, target)
        last = self._t.get(k)
        if last is not None:
            try:
                if (now - timeutil.parse_ts(last)).total_seconds() < self.window_s:
                    return False
            except ValueError:
                pass
        self._t[k] = timeutil.format_ts(now)
        self._prune(now)
        self._save()
        return True

    def _prune(self, now: datetime) -> None:
        limit = self.window_s * KEEP_FACTOR
        for k in list(self._t):
            try:
                if (now - timeutil.parse_ts(self._t[k])).total_seconds() > limit:
                    del self._t[k]
            except ValueError:
                del self._t[k]

    def __len__(self) -> int:
        return len(self._t)

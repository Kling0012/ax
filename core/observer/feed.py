"""事象の購読（kind ごとの読み位置を持つ）。検査が新しい事象だけを見るための部品。

読み位置は自分のボリュームの cursors.json。初回（位置が無い）は、投影の upto_seq から backfill 件ぶん戻った所から読む
（再起動後に直近の様子を取り戻す。GET /v1/events に「since」が無いため。S-3.5）。
"""
from __future__ import annotations

from pathlib import Path

from .sources import Sources, json_load, json_save, order_key

MAX_PAGES = 10
PAGE = 500


class EventFeed:
    def __init__(self, sources: Sources, path: str | Path, backfill: int = 2000):
        self.sources = sources
        self.path = Path(path)
        self.backfill = backfill
        self._bootstrapped: set[str] = set()
        self._cur: dict[str, int] = {k: int(v) for k, v in json_load(self.path, {}).items() if isinstance(v, int)}

    def poll(self, kind: str, tail_seq: int) -> list[dict]:
        """kind の新しい事象（seq の昇順）。読み位置を進めて保存する。Unavailable は呼び出し側へ出す。"""
        after = self._cur.get(kind)
        if kind not in self._bootstrapped:
            # 検査用の Memory は揮発性なので、再起動時は直近の事象から作り直す。
            after = max(0, tail_seq - self.backfill)
        if after is None:
            after = max(0, tail_seq - self.backfill)
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            page = self.sources.events_after(kind, after, PAGE)
            if not page:
                break
            out.extend(page)
            after = max(after, max(order_key(e) for e in page))
            if len(page) < PAGE:
                break
        self._bootstrapped.add(kind)
        if after != self._cur.get(kind):
            self._cur[kind] = after
            json_save(self.path, self._cur)
        return out

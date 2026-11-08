"""ax-admin（timeline の管理用。ホストの中だけ。D-3）。

  python -m core.timeline.admin rebuild-projections [--db PATH] [--archive DIR] [--verify]

rebuild-projections: projections を空にして、退避済みの区間（archive の JSON Lines。seq の順）を先に、次に DB の事象を seq の順に読み直す。
--verify: 作り直す前の表と、すべての行で value と upto_seq が一致することを確かめる（違えば終了コード 2。表は作り直した後の物）。
終了コード: 0 成功 / 1 作り直せない（書き出しが壊れている等。表は元のまま）/ 2 --verify で不一致。
ネットワークは使わない。稼働中の timeline と同じ DB を開いてよい（作り直しの間だけ追記が待たされる）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .archive import ArchiveError, TimelineArchive
from .store import RebuildError, Store


def rebuild(db: str, archive_dir: str | None, verify: bool = False) -> tuple[int, dict]:
    store = Store(db)
    try:
        before = store.projection_snapshot() if verify else None
        arch = TimelineArchive(archive_dir) if archive_dir else None
        try:
            res = store.rebuild_projections(arch.iter_events() if arch else None)
        except (RebuildError, ArchiveError) as e:
            return 1, {"error": str(e)}
        if verify:
            after = store.projection_snapshot()
            diff = sorted({k for k in set(before) | set(after) if before.get(k) != after.get(k)})
            res["verified"] = not diff
            if diff:
                res["differences"] = [f"{n}/{k}" for n, k in diff[:20]]
                return 2, res
        return 0, res
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ax-admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("rebuild-projections")
    rp.add_argument("--db", default=os.environ.get("AX_TIMELINE_DB", "/data/timeline.sqlite"))
    rp.add_argument("--archive", default=os.environ.get("AX_TIMELINE_ARCHIVE", "/data/archive"))
    rp.add_argument("--verify", action="store_true")
    a = ap.parse_args(argv)
    code, res = rebuild(a.db, a.archive, a.verify)
    print(json.dumps(res, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())

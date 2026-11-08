"""時系列記録の書き出しと、書き出した区間の読み出し（S-14.3 末尾、D-3）。

書き出し: seq が連続する区間を、日付が変わるごとに分けて JSON Lines にし、`<archive>/timeline/` に置く。
  ファイル名: timeline-YYYY-MM-DD-<先頭の seq（12 桁）>.jsonl（1 行 1 事象。seq を含む）。
  索引: `<archive>/timeline/index.json`（書き出し済みの区間の一覧と SHA-256。書き出した区間は seq の 1 から連続する）。
  ファイルを先に書き、索引を最後に書く（索引が確定の印。途中で落ちたファイルは次回に同じ名前で書き直す）。
退避コネクタ（archiver）が、各ボリュームの archive/ にある古いファイルを退避先へ移す。索引は移さない（archiver の keep_original の既定）。
DB の事象は消さない（削除の経路を持たない）。作り直しと古い範囲の照会は、索引のファイルが手元にある範囲で行う。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterator

from axcommon import timeutil

from .store import RebuildError, Store

SUBDIR = "timeline"
INDEX_NAME = "index.json"


class ArchiveError(Exception):
    pass


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class TimelineArchive:
    def __init__(self, archive_dir: str | Path):
        self.root = Path(archive_dir)
        self.dir = self.root / SUBDIR
        self.index_path = self.dir / INDEX_NAME

    # --- 索引 -----------------------------------------------------------------------------------
    def load_index(self) -> dict:
        try:
            doc = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "files": []}
        except (OSError, ValueError) as e:
            raise ArchiveError(f"索引が読めない: {type(e).__name__}") from e
        if not isinstance(doc, dict) or not isinstance(doc.get("files"), list):
            raise ArchiveError("索引の形が違う")
        return doc

    def last_seq(self) -> int:
        files = self.load_index()["files"]
        return max((f["last_seq"] for f in files), default=0)

    # --- 書き出し -------------------------------------------------------------------------------
    def export(self, store: Store, *, before_ts: str | None = None, to_seq: int | None = None, batch: int = 2000) -> dict:
        """前回の続きから、条件に合う事象を書き出す。条件に合わない事象が来たところで止める（区間を連続に保つ）。
        before_ts は ts がこれより前（排他）、to_seq はこの seq まで（含む）。戻り値は {exported, files, last_seq}。"""
        index = self.load_index()
        watermark = max((f["last_seq"] for f in index["files"]), default=0)
        written: list[dict] = []
        total = 0
        cur = watermark
        group: list[dict] = []
        group_date: str | None = None

        def flush_group() -> None:
            nonlocal group, group_date
            if not group:
                return
            first, last = group[0]["seq"], group[-1]["seq"]
            name = f"timeline-{group_date}-{first:012d}.jsonl"
            body = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in group).encode("utf-8")
            self.dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.dir / name, body)
            written.append({"file": name, "date": group_date, "first_seq": first, "last_seq": last, "count": len(group),
                            "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
            group, group_date = [], None

        stop = False
        while not stop:
            chunk = store.list_events(after=cur, limit=batch)
            if not chunk:
                break
            for ev in chunk:
                if (to_seq is not None and ev["seq"] > to_seq) or (before_ts is not None and ev["ts"] >= before_ts):
                    stop = True
                    break
                day = ev["ts"][:10]
                if group_date is not None and day != group_date:
                    flush_group()
                group_date = day
                group.append(ev)
                total += 1
                cur = ev["seq"]
            else:
                continue
        flush_group()
        if written:
            index["files"] = sorted(index["files"] + written, key=lambda f: f["first_seq"])
            index["updated_at"] = timeutil.now_ts()
            _atomic_write(self.index_path, (json.dumps(index, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        return {"exported": total, "files": [w["file"] for w in written], "last_seq": max(watermark, cur)}

    # --- 読み出し -------------------------------------------------------------------------------
    def iter_events(self, *, verify: bool = True, after: int = 0) -> Iterator[dict]:
        """手元にある書き出しを seq の順に読む。索引にあるのにファイルが無い区間は飛ばす（退避先へ移った可能性。DB で補う）。
        内容の SHA-256 が索引と違うファイルは RebuildError（壊れた書き出しで表を作らない）。"""
        for f in self.load_index()["files"]:
            if f["last_seq"] <= after:
                continue
            p = self.dir / f["file"]
            if not p.is_file():
                continue
            if verify and _sha256_file(p) != f["sha256"]:
                raise RebuildError(f"書き出しの内容が索引と違う: {f['file']}")
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    if ev["seq"] > after:
                        yield ev

    def missing_files(self) -> list[str]:
        return [f["file"] for f in self.load_index()["files"] if not (self.dir / f["file"]).is_file()]

    def read_from_destination(self, *_a, **_k):
        """退避先へ移った区間の読み戻し。誰が鍵を持つかが文書で決まらない（OPEN.md: S-14.3 と D-3）。"""
        raise NotImplementedError("OPEN: 退避先の書き出しからの照会は決まっていない（build/OPEN.md の b1 の項）")

"""要求ファイル（`${ax_state_dir}/host-requests/`）の読み取りと検査。書き方は core/observer/hostreq.py（形の正本）。

実行するのは、操作が restart、deploy、seat-sync のどれかで、restart の名前が登録済みの compose のサービスに当たる要求だけ（S-11.6）。
それ以外は結果 rejected を記録して host-requests/rejected/ へ移す。処理した要求は host-requests/done/ へ移す（削除しない）。
ファイルは通常のファイル（シンボリックリンクは不可）、512 バイト以下、所有者が自分（operator）の物だけ読む。
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from core.observer import hostreq


@dataclass
class Request:
    path: Path
    op: str | None
    name: str | None
    error: str | None = None  # 形の不備の理由（定型文）


def read_requests(directory: Path, limit: int) -> list[Request]:
    out: list[Request] = []
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(hostreq.SUFFIX) and not n.startswith("."))
    except OSError:
        return out
    uid = os.getuid()
    for n in names[:limit]:
        p = directory / n
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            out.append(Request(p, None, None, "not_regular_file"))
            continue
        if st.st_uid != uid:
            out.append(Request(p, None, None, "foreign_owner"))
            continue
        if st.st_size > hostreq.MAX_BYTES:
            out.append(Request(p, None, None, "too_large"))
            continue
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                opened = os.fstat(f.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_uid != uid:
                    raise ValueError("invalid request file")
                raw = f.read(hostreq.MAX_BYTES + 1)
            op, name = hostreq.parse_body(raw)
            if json.loads(raw) != hostreq.build_body(op, name):
                raise ValueError("unexpected request fields")
        except (OSError, ValueError):
            out.append(Request(p, None, None, "bad_form"))
            continue
        out.append(Request(p, op, name))
    return out


def archive(req: Request, directory: Path, bucket: str) -> None:
    """要求を host-requests/<bucket>/ へ移す（削除しない）。"""
    dest = directory / bucket
    try:
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / req.path.name
        if target.exists():
            target = dest / (req.path.name + "." + str(os.getpid()))
        os.rename(req.path, target)
    except OSError:
        pass

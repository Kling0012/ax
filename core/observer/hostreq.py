"""ホスト見張りへの要求ファイル（S-11.6、D-10.3、F-18.3）。標準ライブラリと axcommon.ids だけ。

置き場: `/state/host-requests/`（コンテナ側。ホストの `${ax_state_dir}/host-requests/`）。1 要求 = 1 ファイル `<ULID>.req`、中身は 1 行の JSON。
  {"op": "restart", "name": "<compose のサービス名>"}    書けるのは observer と emergency
  {"op": "deploy"}                                         書けるのは intake（名前なし）
  {"op": "seat-sync"}                                      書けるのは intake（名前なし）
要求にはイメージ名、タグ、パス、サービスの定義を書けない（キーはこの 2 つだけ。ほかのキーがあれば受け側が rejected にする）。
書き方: 一時ファイル（先頭が "."）に書いて rename する。受け側は "." で始まる名前と .req 以外を読まない。
文書に形の指定が無いため b8 が決めた（OPEN.md の b8、docs/b8/host-requests.md）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from axcommon import ids

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
OPS_WITH_NAME = frozenset({"restart"})
OPS_WITHOUT_NAME = frozenset({"deploy", "seat-sync"})
MAX_BYTES = 512
SUFFIX = ".req"


def build_body(op: str, name: str | None = None) -> dict:
    if op in OPS_WITH_NAME:
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError("restart には compose のサービス名が要る")
        return {"op": op, "name": name}
    if op in OPS_WITHOUT_NAME:
        if name is not None:
            raise ValueError(f"{op} に名前は付けない")
        return {"op": op}
    raise ValueError(f"知らない操作: {op}")


def parse_body(raw: bytes) -> tuple[str, str | None]:
    """要求の中身 -> (op, name)。形が違えば ValueError（受け側は rejected にする）。"""
    if len(raw) > MAX_BYTES:
        raise ValueError("要求が大きすぎる")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("JSON でない") from None
    if not isinstance(obj, dict):
        raise ValueError("オブジェクトでない")
    extra = set(obj) - {"op", "name"}
    if extra:
        raise ValueError("許されないキーがある")
    op = obj.get("op")
    if not isinstance(op, str):
        raise ValueError("op が無い")
    if op in OPS_WITHOUT_NAME and "name" in obj:
        raise ValueError("名前なしの操作に name は付けない")
    body = build_body(op, obj.get("name"))
    return op, body.get("name")


def write_request(directory: str | os.PathLike, op: str, name: str | None = None) -> Path:
    body = build_body(op, name)
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    rid = ids.new_ulid()
    tmp = d / f".{rid}.tmp"
    final = d / f"{rid}{SUFFIX}"
    line = json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, final)
    except BaseException:
        # 失敗した一時ファイルは残す。受け側は先頭が "." の名前を読まない。
        raise
    return final


def pending(directory: str | os.PathLike) -> list[tuple[str, str | None]]:
    """まだ処理されていない要求（形が正しい物だけ）。重複して書かないための確認に使う。"""
    out = []
    try:
        for p in sorted(Path(directory).glob("*" + SUFFIX)):
            if p.name.startswith("."):
                continue
            try:
                out.append(parse_body(p.read_bytes()))
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out

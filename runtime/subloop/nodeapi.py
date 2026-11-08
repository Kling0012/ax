"""部品庫の型が使う道具（D-6.3）。`plugins/nodes/<名前空間>/<型>.py` はここから import する。

型のモジュールの約束:
    schema  : `with` の JSON Schema（dict）
    META    : 型の宣言（下の DEFAULT_META の項目。省略した項目は既定）
    async def run(ctx, inp) -> dict      S-7.3 の出力 {"status", "data", "artifacts", "error"}
    async def reconcile(ctx, key) -> dict | None   外へ影響する型だけ（二重実行の防止。D-6.2）

出力は ok()/bug()/error()/skip()/eject() で作る。
"""
from __future__ import annotations

from typing import Any

DEFAULT_META: dict[str, Any] = {
    "statuses": ["ok", "bug", "error"],  # この型が返しうる status。bug を含む型は失敗時の行き先が要る（S-7.7 の 6）
    "outcomes": [],                      # 宣言した結果名（`when: outcome:<名>`）
    "output": None,                      # 外へ影響する操作の種類（manifest の outputs に載せる。例 github.comment）
    "publishes": False,                  # 外へ公開する型（前に gate.secret_scan が要る。S-7.7 の 8）
    "launching": False,                  # 外部操作の直前に launching を書く型（reconcile を持つ）
    "llm": False,                        # LLM を使う
    "role_field": None,                  # with の中で worker の役割を指す項目名（グラフ検査の 9、10）
    "writes_repo": False,                # リポジトリを書き換える
}


class EjectRequest(Exception):
    """ノードが「この項目を外へ出す」と決めたときの合図（blocked など）。run() は eject() で辞書を返してもよい。"""

    def __init__(self, reason: str, *, todo: dict | None = None, artifacts: dict | None = None, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason
        self.todo = todo
        self.artifacts = artifacts or {}


def _out(status: str, data: dict | None, artifacts: dict | None, error: dict | None) -> dict:
    o: dict[str, Any] = {"status": status, "data": data or {}}
    if artifacts:
        o["artifacts"] = artifacts
    if error:
        o["error"] = error
    return o


def ok(data: dict | None = None, artifacts: dict | None = None) -> dict:
    return _out("ok", data, artifacts, None)


def skip(data: dict | None = None, artifacts: dict | None = None) -> dict:
    return _out("skip", data, artifacts, None)


def bug(code: str, message: str = "", data: dict | None = None, artifacts: dict | None = None) -> dict:
    """成果物のバグ（artifact.bug）。"""
    return _out("bug", data, artifacts, {"class": "artifact.bug", "code": code, "message": message[:500]})


def error(code: str, message: str = "", *, expected: bool = False, data: dict | None = None) -> dict:
    """システム側のエラー。expected=True は system.expected（再試行の対象）。"""
    return _out("error", data, None, {"class": "system.expected" if expected else "unknown", "code": code, "message": message[:500]})


def eject(reason: str, *, todo: dict | None = None, artifacts: dict | None = None, data: dict | None = None) -> dict:
    """項目を外へ出す（再試行も on_fail も使わない）。実行系が `_eject` を読んで項目を ejected にする。"""
    o = _out("error", data, None, {"class": "unknown", "code": reason, "message": ""})
    o["_eject"] = {"reason": reason, "todo": todo, "artifacts": artifacts or {}}
    return o


def merge_meta(meta: dict | None) -> dict:
    m = dict(DEFAULT_META)
    m.update(meta or {})
    return m

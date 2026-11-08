"""呼び出し元の資格（F-18.4、S-9.5）。

- 作業側の口（8081）: プラグインごとのトークンのハッシュの一覧（F-18.4。${ax_state_dir}/<ハッシュ一覧>）で
  呼び出し元のプラグイン ID を決める。一覧は `axcommon.creds.TokenTable`（1 行 `<名前> <sha256>`）と同じ形。
  置き場は環境変数 AX_PLUGIN_TOKENS（既定 ${AX_STATE_DIR}/<ハッシュ一覧>。ファイル名は文書で伏せ字。OPEN.md）。
- 中核の口（8080）: core-tokens の表（F-18.4 の ${ax_state_dir}/core-tokens.sha256）で呼び出し元の部品名を決める。
  環境変数 AX_CORE_TOKENS（既定 ${AX_STATE_DIR}/core-tokens.sha256）。
どちらも mtime が変われば読み直す（TokenTable.reload_if_changed）。秘密の値はここに出さない。
"""
from __future__ import annotations

import os
from pathlib import Path

from axcommon.creds import TokenTable, parse_bearer, sha256_hex

HASH_LIST_BASENAME = "plugin-tokens.sha256"  # F-18.4 の一覧の既定の名（文書は伏せ字。OPEN.md の b0/b3 の項）


def plugin_table(path: str | os.PathLike | None = None) -> TokenTable:
    """作業側の口のプラグインのトークン表（遅延 作成。読めなければ空の表）。"""
    p = Path(path) if path else Path(os.environ.get("AX_STATE_DIR", "/state")) / HASH_LIST_BASENAME
    if "AX_PLUGIN_TOKENS" in os.environ and path is None:
        p = Path(os.environ["AX_PLUGIN_TOKENS"])
    if not p.is_file():
        return TokenTable(entries={}, path=p)
    return TokenTable.from_file(p)


def core_table(path: str | os.PathLike | None = None) -> TokenTable:
    p = Path(path) if path else Path(os.environ.get("AX_CORE_TOKENS",
                                                    str(Path(os.environ.get("AX_STATE_DIR", "/state")) / "core-tokens.sha256")))
    if not p.is_file():
        return TokenTable(entries={}, path=p)
    return TokenTable.from_file(p)


def identify_plugin(table: TokenTable, header: str | None) -> str | None:
    """Authorization: Bearer <トークン> からプラグイン ID を決める。不明は None（401）。"""
    return table.identify(parse_bearer(header))


def reload_if_changed(table: TokenTable) -> None:
    table.reload_if_changed()

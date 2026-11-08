"""呼び出し元の資格から issued_by を決める（S-5.3、D-4）。

- manager / backup / inspection / emergency: それぞれの部品のコンテナだけが持つ専用トークン
  （compose の secrets: control-token-manager など。b0 の DECISIONS）。トークンの sha256 を定数時間で比べ、
  資格の名前に固定する。本文の issued_by は照合だけに使う。
- user / commander: 入口（intake）が受け口で決めて中継する。core-tokens の表（F-18.4）の `intake` の資格だけが
  本文で user / commander を主張できる。表の置き場は環境変数 AX_CORE_TOKENS（既定 ${AX_STATE_DIR}/core-tokens.sha256）。
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

from axcommon.creds import TokenTable, parse_bearer, read_secret_optional, sha256_hex

from . import INTAKE_CALLER, INTAKE_ISSUERS, ISSUER_TOKENS


def issuer_for(header_value: str | None, *, declared: str | None = None,
               core_tokens: TokenTable | None = None, secret_values: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Authorization の生値から (issued_by, 根拠) を決める。決まらなければ (None, 理由)。

    secret_values は試験用の差し替え（既定は /run/secrets/<名前> を読む。AX_SECRETS_PATH で置き場を変えられる）。
    """
    token = parse_bearer(header_value)
    if not token:
        return None, "no_token"
    values = secret_values if secret_values is not None else {
        name: read_secret_optional(name) for name in sorted(set(ISSUER_TOKENS.values()))}
    digest = sha256_hex(token)
    for issued_by, name in ISSUER_TOKENS.items():
        value = values.get(name)
        if value and hmac.compare_digest(digest, sha256_hex(value)):
            return issued_by, "control-token"
    if core_tokens is None:
        core_tokens = _core_table()
    core_tokens.reload_if_changed()
    caller = core_tokens.identify(token)
    if caller == INTAKE_CALLER and declared in INTAKE_ISSUERS:
        return declared, "intake-relay"
    if caller is not None:
        return None, "caller_cannot_issue"
    return None, "unknown_token"


def _core_table() -> TokenTable:
    p = Path(os.environ.get("AX_CORE_TOKENS",
                            str(Path(os.environ.get("AX_STATE_DIR", "/state")) / "core-tokens.sha256")))
    if not p.is_file():
        return TokenTable(entries={})
    return TokenTable.from_file(p)


def core_table() -> TokenTable:
    return _core_table()

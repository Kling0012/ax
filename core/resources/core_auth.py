"""core-tokens の資格の検証（F-18.4、S-25.27、D-23）。

中核の部品が別の中核の部品の書き込み系を呼ぶときの資格。ホストの
${secrets_dir}/core-tokens/<部品名>（32 バイトの乱数の 16 進、権限 600）を呼ぶ側の
コンテナにだけ compose の secrets で渡し、呼ばれる側は ${ax_state_dir}/core-tokens.sha256
（読み取り専用の bind。1 行 1 件 <名前> <sha256>）のハッシュだけを持つ。

- TokenTable は axcommon.creds を使う（1 行 1 件 <名前> <sha256 の 16 進>）。
- 許可の表は API ごとに、許す資格名を固定する。例えば gate-token と revoke は worker-pool だけ。
- 起動時と一定間隔（既定 60 秒）で読み直す。読めないときは表を保ち、health で出す。
- 値はログ・例外・応答に出さない。定数時間で比較する（axcommon.creds.TokenTable.identify）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from axcommon import creds

# API -> 許す資格名の一覧（S-25.27、F-18.4）。載っていない API は許可しない。
CREDENTIAL_SCOPES: dict[str, tuple[str, ...]] = {
    "secret-push": ("intake",),
    "slot-cap": ("control",),
    "model-runners": ("control",),
    "command-exclusion": ("control",),
    "environment-lease": ("worker-pool",),
    "slot-reserve": ("worker-pool", "release-review"),
    "gate-token": ("worker-pool",),
    "gate-token-revoke": ("worker-pool",),
    "envs-select-path-g": ("worker-pool",),
}


class CoreAuth:
    def __init__(self, table: str | os.PathLike | creds.TokenTable | None = None, *,
                 reload_interval_s: float = 60.0, time_fn: Callable[[], float] = time.monotonic):
        if table is None:
            path = os.environ.get("AX_CORE_TOKENS_SHA256") or "/state/core-tokens.sha256"
            self.table = creds.TokenTable.from_file(path)
        elif isinstance(table, creds.TokenTable):
            self.table = table
        else:
            self.table = creds.TokenTable.from_file(table)
        self._interval = reload_interval_s
        self._time = time_fn
        self._last_reload = self._time()

    def refresh_if_due(self) -> bool:
        if self._time() - self._last_reload >= self._interval:
            ok = self.table.reload()
            self._last_reload = self._time()
            return ok
        return False

    def identify(self, header: str | None) -> str | None:
        """Authorization ヘッダーから資格名を返す。ヘッダーが無い、一覧に無い、一致しないときは None。"""
        token = creds.parse_bearer(header or "")
        if not token:
            return None
        return self.table.identify(token)

    def allows(self, credential: str, api: str) -> bool:
        """この資格がこの API を呼んでよいか。"""
        allowed = CREDENTIAL_SCOPES.get(api)
        if allowed is None:
            return False
        return credential in allowed

    def check(self, header: str | None, api: str) -> tuple[str | None, str]:
        """(資格名, 結果) を返す。結果は ok / unauthenticated / credential_scope。"""
        self.refresh_if_due()
        cred = self.identify(header)
        if cred is None:
            return None, "unauthenticated"
        if not self.allows(cred, api):
            return cred, "credential_scope"
        return cred, "ok"

    def health(self) -> dict:
        return {"core_tokens": self.table.names(), "table_ok": bool(self.table.names())}

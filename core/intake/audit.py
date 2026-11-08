"""受け口の監査ログ（F-9.2: `${ax_state_dir}/commander/access.log`。intake が書く追記専用）。

B5 は最小版: commander ソケットへの要求 1 件につき 1 行の JSON（ts、via、principal、method、path、status）。
集計（`commander.read.summary`）と詳しい規則は S-6.20（後続の束 b12b3）の担当。
秘密の値は書かない。本文そのものは書かない。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class AccessLog:
    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def write(self, *, ts: str, via: str | None, principal: str | None, method: str, path: str, status: int) -> None:
        if self.path is None:
            return
        line = json.dumps({"ts": ts, "via": via, "principal": principal, "method": method, "path": path,
                           "status": int(status)}, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass  # 監査ログの失敗で指示を止めない（fail-soft）

    def read_all(self) -> list[dict]:
        if self.path is None or not self.path.is_file():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

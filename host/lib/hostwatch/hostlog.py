"""hostwatch.log（`${ax_state_dir}/hostwatch.log`）への書き込みと journal への出力。

ログの 1 行は S-3.2 の形の事象（JSON）。ホストは時系列記録に届かないので、ここへ書く。オブザーバーが戻ったら時系列記録へ写す（D-10.3 の 6）。
書くのは操作名、対象、結果だけ。値は書かない。journal へは同じ行の要約を標準エラーに出す（systemd が journal へ送る）。
追記は flock で排他する（ax-up.sh、ax-deploy.sh、ax-redact-index.sh からも書く）。書けなくても処理は止めない。
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Callable

from axcommon import events as ax_events


class HostLog:
    def __init__(self, path: str | os.PathLike, host: str, *, stream=None, component: str = "hostwatch"):
        self.path = Path(path)
        self.host = host
        self.component = component
        self.stream = sys.stderr if stream is None else stream
        self.failures = 0

    def event(self, kind: str, subject: str, data: dict | None = None, *, cause: str | None = None) -> dict | None:
        try:
            ev = ax_events.build_event(component=self.component, host=self.host, kind=kind, subject=subject, data=data or {}, cause=cause)
        except Exception:  # noqa: BLE001  形が作れない事象でも処理は止めない
            self.failures += 1
            return None
        line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                os.fchmod(f.fileno(), 0o600)
                f.write(line)
                f.flush()
        except OSError:
            self.failures += 1
        short = f"{kind} {subject} " + " ".join(f"{k}={v}" for k, v in sorted((data or {}).items()) if isinstance(v, (str, int, float, bool)))
        try:
            print("ax-hostwatch:", short, file=self.stream, flush=True)
        except (OSError, ValueError):
            pass
        return ev


def main(argv: list[str] | None = None) -> int:
    """`python3 -P -m hostwatch.hostlog <state_dir> <host> <kind> <subject> [key=value …]` — bash のスクリプトから事象を 1 行足す。"""
    a = sys.argv[1:] if argv is None else argv
    if len(a) < 4:
        print("usage: hostlog STATE_DIR HOST KIND SUBJECT [key=value ...]", file=sys.stderr)
        return 2
    state, host, kind, subject = a[:4]
    data: dict = {}
    for kv in a[4:]:
        k, sep, v = kv.partition("=")
        if not sep:
            return 2
        data[k] = v
    ev = HostLog(Path(state) / "hostwatch.log", host).event(kind, subject, data)
    return 0 if ev else 1


if __name__ == "__main__":
    sys.exit(main())

"""コマンドの実行器。本番は SubprocessRunner、試験は同じ形の偽物に差し替える（tests/fakes/b8/fake_runner.py）。"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


class SubprocessRunner:
    def run(self, argv: list[str], *, timeout: float | None = 120, cwd: str | None = None, env: dict | None = None) -> Result:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env, check=False)
        except FileNotFoundError:
            return Result(127, "", "command not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", "timeout")
        except OSError as e:
            return Result(126, "", type(e).__name__)
        return Result(p.returncode, p.stdout or "", p.stderr or "")

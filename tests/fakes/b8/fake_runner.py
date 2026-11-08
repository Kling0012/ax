"""偽の実行器（host/lib/hostwatch の Runner と同じ形）。実際の sudo・docker・git は呼ばない。組み立てたコマンドを記録し、規則で結果を返す。

  r = FakeRunner()
  r.on("docker compose", Result(0, out))            # argv を空白でつないだ文字列に部分一致（後から足した規則が先）
  r.on(lambda argv: argv[:2] == ["sudo", "-n"] and argv[2:] == ["true"], Result(1))
  r.calls                                            # 呼ばれた argv の一覧
  r.cmds("up")                                       # 文字列に "up" を含む呼び出し（空白区切りの文字列）
規則に当たらない呼び出しは Result(0, "")。
"""
from __future__ import annotations

from typing import Callable

from hostwatch.runner import Result


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.rules: list[tuple[Callable[[list[str]], bool], Result | Callable[[list[str]], Result]]] = []

    def on(self, match, result):
        fn = match if callable(match) else (lambda argv, m=match: m in " ".join(argv))
        self.rules.insert(0, (fn, result))
        return self

    def run(self, argv, *, timeout=None, cwd=None, env=None) -> Result:
        self.calls.append(list(argv))
        self.envs.append(env)
        for fn, res in self.rules:
            if fn(argv):
                return res(argv) if callable(res) else res
        return Result(0, "", "")

    def cmds(self, needle: str = "") -> list[str]:
        return [" ".join(c) for c in self.calls if needle in " ".join(c)]

    def privileged(self) -> list[str]:
        """sudo を前置きした呼び出し（`sudo -n true` の事前確認を除く）。"""
        return [" ".join(c) for c in self.calls if c[:2] == ["sudo", "-n"] and c[2:] != ["true"]]

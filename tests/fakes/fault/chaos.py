"""プロセスの中の部品を壊す（B13）。オブジェクトのメソッドを差し替えて、例外・遅延・でたらめな値・固まりを注入する。

    chaos = Chaos(clock=fake_clock)
    chaos.raise_on(obj, "run_once", RuntimeError("boom"), times=None)   # 呼ぶたびに例外（times 回だけも可）
    chaos.garbage(obj, "effective", {"mode": "???"})                    # でたらめな値を返す
    chaos.slow(obj, "tick", 30)                                         # clock を 30 秒進めてから本物を呼ぶ（同期のみ）
    chaos.replace(obj, "emit", lambda orig, kind, *a, **k: orig("instruction.received", *a, **k))   # 呼び出しを書き換える
    chaos.hang(obj, "run_once")                                         # 非同期: 終わらない（asyncio.sleep の長い待ち）。同期: TimeoutError
    chaos.restore()                                                     # すべて元へ戻す
同期・非同期の両方のメソッドを扱う。壊した回数は chaos.hits[(id, name)]。
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any


class Chaos:
    def __init__(self, clock: Any = None):
        self.clock = clock
        self._saved: list[tuple[Any, str, Any, bool]] = []
        self.hits: dict[tuple[int, str], int] = {}

    def raise_on(self, obj, name, exc: BaseException | None = None, *, times: int | None = None) -> None:
        exc = exc or RuntimeError("chaos")
        state = {"n": 0}

        def behave(orig, is_async, a, k):
            if times is None or state["n"] < times:
                state["n"] += 1
                if is_async:
                    async def _r():
                        raise exc
                    return _r()
                raise exc
            return orig(*a, **k)

        self._wrap_passthrough(obj, name, behave)

    def garbage(self, obj, name, value: Any) -> None:
        def behave(orig, is_async, a, k):
            if is_async:
                async def _r():
                    return value
                return _r()
            return value

        self._wrap_passthrough(obj, name, behave)

    def slow(self, obj, name, seconds: float) -> None:
        def behave(orig, is_async, a, k):
            if is_async:
                async def _r():
                    await asyncio.sleep(0)
                    if self.clock is not None:
                        self.clock.sleep(seconds)
                    return await orig(*a, **k)
                return _r()
            if self.clock is not None:
                self.clock.sleep(seconds)
            return orig(*a, **k)

        self._wrap_passthrough(obj, name, behave)

    def hang(self, obj, name) -> None:
        def behave(orig, is_async, a, k):
            if is_async:
                async def _r():
                    await asyncio.sleep(10 ** 6)
                return _r()
            raise TimeoutError("chaos: hang")

        self._wrap_passthrough(obj, name, behave)

    def replace(self, obj, name, fn) -> None:
        """メソッドを fn(orig, *args, **kw) に差し替える（呼び出しを書き換える故障。例: 事象の kind を許されない物にする）。"""
        orig = getattr(obj, name)
        had_own = name in getattr(obj, "__dict__", {})
        self._saved.append((obj, name, orig, had_own))
        self.hits.setdefault((id(obj), name), 0)
        key = (id(obj), name)

        def wrapper(*a, **k):
            self.hits[key] += 1
            return fn(orig, *a, **k)

        setattr(obj, name, wrapper)

    def _wrap_passthrough(self, obj, name, behave) -> None:
        """behave(orig, is_async, a, k) が値かコルーチンを返す形に合わせる。"""
        orig = getattr(obj, name)
        had_own = name in getattr(obj, "__dict__", {})
        self._saved.append((obj, name, orig, had_own))
        key = (id(obj), name)
        self.hits.setdefault(key, 0)
        if inspect.iscoroutinefunction(orig):
            async def wrapper(*a, **k):
                self.hits[key] += 1
                return await behave(orig, True, a, k)
        else:
            def wrapper(*a, **k):
                self.hits[key] += 1
                return behave(orig, False, a, k)
        setattr(obj, name, wrapper)

    def restore(self) -> None:
        for obj, name, orig, had_own in reversed(self._saved):
            if had_own:
                setattr(obj, name, orig)
            else:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
        self._saved.clear()

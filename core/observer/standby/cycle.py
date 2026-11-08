"""管理役の周回の呼び出し口（D-10.2）。

予備役は「管理役と同じ入力・同じ操作の一覧」で管理役の周回の関数を 1 回呼ぶ（S-11.3 の 2）。その関数の名前と引数は
b9（core/manager）が決める。文書には書かれていないため、この束は次の約束を仮に置く（OPEN.md の b10a）:

    core.manager.cycle.run_cycle(*, role: str, issued_by: str, trigger: dict, emit_cycle: bool = False) -> dict | object

  role         "backup_manager"（worker-pool の役割。S-8.3）
  issued_by    "backup"（制御 API の専用トークンは control-token-backup。管理役のモジュールが読む）
  trigger      {"check", "target", "detail"}（警報の中身。値や秘密は入れない）
  emit_cycle   False のとき、周回の関数は `manager.cycle` を書かない（予備役の側が 1 件だけ書く）
  戻り値       {"ok": bool, "actions": int, "reason": str|None, "cycle_id": str|None, "model": str|None}
               （属性で持つ物でもよい）

その関数が無ければ NotImplementedError（予備役の呼び出しは失敗として扱い、根本原因の ToDo だけは必ず作る）。
試験は CycleRunner を満たす偽の物（tests/fakes/b10a/fake_manager.py）を渡す。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import ISSUED_BY, ROLE


@dataclass(frozen=True)
class CycleRequest:
    trigger: dict
    role: str = ROLE
    issued_by: str = ISSUED_BY
    emit_cycle: bool = False


@dataclass
class CycleResult:
    ok: bool
    actions: int = 0
    reason: str | None = None
    cycle_id: str | None = None
    model: str | None = None
    extra: dict = field(default_factory=dict)


class CycleRunner(Protocol):
    def run(self, req: CycleRequest) -> CycleResult: ...


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def normalize(raw: Any) -> CycleResult:
    if isinstance(raw, CycleResult):
        return raw
    if raw is None:
        return CycleResult(ok=True)
    actions = _get(raw, "actions", 0)
    if isinstance(actions, (list, tuple)):
        actions = len(actions)
    return CycleResult(ok=bool(_get(raw, "ok", True)), actions=int(actions or 0), reason=_get(raw, "reason"),
                       cycle_id=_get(raw, "cycle_id"), model=_get(raw, "model"))


class ManagerModuleRunner:
    """core.manager.cycle.run_cycle を呼ぶ。モジュールか関数が無ければ NotImplementedError。"""

    def __init__(self, module: str = "core.manager.cycle", function: str = "run_cycle"):
        self.module, self.function = module, function

    def run(self, req: CycleRequest) -> CycleResult:
        try:
            mod = importlib.import_module(self.module)
        except ImportError as e:
            raise NotImplementedError(f"{self.module} が無い（b9 の周回の関数が未提供）") from e
        fn = getattr(mod, self.function, None)
        if not callable(fn):
            raise NotImplementedError(f"{self.module}.{self.function} が無い（b9 の周回の関数が未提供）")
        raw = fn(role=req.role, issued_by=req.issued_by, trigger=dict(req.trigger), emit_cycle=req.emit_cycle)
        return normalize(raw)

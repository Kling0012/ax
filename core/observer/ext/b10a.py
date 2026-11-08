"""b10a の差し込み: 予備役（S-11.3）を、オブザーバーの宛先 `backup` の呼び出しとして登録する。

axcommon.ext.load_extensions が register(app) を呼ぶ。app.state.observer（Observer）が入っていること。
失敗してもこの拡張だけが止まる（fail-soft）。予備役が無い間、オブザーバーは `backup_unavailable` を数えるだけで続ける（b8）。
"""
from __future__ import annotations

import os
from pathlib import Path

from axcommon.client import LedgerClient
from axcommon.creds import read_secret_optional

from core.observer.standby.backup import Standby
from core.observer.standby.cycle import ManagerModuleRunner


def build_standby(obs, *, runner=None, ledger=None, defaults=None) -> Standby:
    s = obs.s
    if defaults is None:
        try:
            from axcommon.defaults import Defaults

            defaults = Defaults.load()
        except Exception:  # noqa: BLE001  config が読めなくても動く（クラスの既定値）
            defaults = None
    window = float(defaults.get("backup_window_days")) if defaults else 7.0
    limit = int(defaults.get("backup_limit")) if defaults else 2
    return Standby(
        runner=runner if runner is not None else ManagerModuleRunner(),
        ledger=ledger if ledger is not None else LedgerClient(s.ledger_url, token=read_secret_optional("core-token-observer")),
        timeline=obs.timeline, sink=obs.sink, data_dir=Path(s.data_dir) / "standby",
        window_days=window, limit=limit,
    )


def register(app) -> None:
    obs = app.state.observer
    standby = build_standby(obs)
    obs.set_hook("backup", standby.on_alert)
    obs.add_check("standby_drain", standby.drain_check)
    app.state.standby = standby

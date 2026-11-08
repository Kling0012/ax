"""管理役の設定。S-17 の値は config/defaults.yaml（axcommon.defaults）から読む。環境変数は置き場と宛先だけ。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from axcommon.defaults import Defaults
from axcommon.client import service_url


@dataclass
class ManagerConfig:
    tick_min: float = 10
    decision_s: float = 300
    longpoll_s: float = 30
    control_max_lease_min: float = 60
    control_max_renewals: int = 3
    timer_max_days: float = 7
    # 期限が近い命令: 次の周回までに切れる物（manager_tick_min 以内）。文書に値が無く、tick と同じにした（DECISIONS）
    near_expiry_min: float = 10
    # LLM が使えない・仕様外の返答のとき、同じ事象を続けて扱う回数の上限。超えたら台帳へ出す（DECISIONS）
    attempts_max: int = 3
    # 1 回の判断に渡す事象の数・data の短縮
    batch_max: int = 25
    summary_str_max: int = 200
    poll_s: float = 2.0                     # worker-pool のジョブの状態を見に行く間隔
    parked_eject_days: float = 1.0          # 台帳へ出すときの check_at の既定（今から）
    host: str = "unknown"
    role: str = "manager"
    state_dir: str = "/state/manager"
    control_token_name: str = "control-token-manager"
    core_token_name: str = "core-token-manager"
    urls: dict = field(default_factory=dict)

    @classmethod
    def load(cls, defaults: Defaults | None = None, environ: dict | None = None) -> "ManagerConfig":
        env = os.environ if environ is None else environ
        d = defaults or Defaults.load()
        tick = float(d.get("manager_tick_min"))
        return cls(
            tick_min=tick, decision_s=float(d.get("manager_decision_s")), longpoll_s=float(d.get("longpoll_s")),
            control_max_lease_min=float(d.get("control_max_lease_min")), control_max_renewals=int(d.get("control_max_renewals")),
            timer_max_days=float(d.get("timer_max_days")), near_expiry_min=tick,
            host=env.get("AX_HOST", "unknown"), state_dir=env.get("AX_MANAGER_STATE_DIR", "/state/manager"),
            urls={n: env.get(f"AX_{n.upper().replace('-', '_')}_URL") or service_url(n) for n in
                  ("timeline", "control", "ledger", "worker-pool", "resources", "memory-connector")},
        )

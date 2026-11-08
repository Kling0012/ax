"""オブザーバーの設定。既定値は S-17（config/defaults.yaml）、場所は環境変数（compose の environment）。

  AX_HOST               事象の source.host
  AX_STATE_DIR          ホストの状態置き場の bind（既定 /state）。observer.beat、host-requests/、hostwatch/registered/、hostwatch.log、cutover/ が下にある
  AX_OBSERVER_DATA      自分のボリューム（既定 /data）。重複除去の表、取り込み済みの位置
  AX_TIMELINE_URL       既定 http://timeline:8080
  AX_LEDGER_URL         既定 http://ledger:8080
  AX_REDACT_INDEX       S-15.3 の索引（既定 /state/redact/index）
  AX_PII_LIST           pii-list（無ければ空）
  AX_OBSERVER_COMMANDER_UNITS  触れない unit 名のカンマ区切り（ホスト見張りの除外と同じ値。ここでは対象から除く名前の照合にだけ使う）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# 中核の部品（compose のサービス名と、health.beat の component が同じ名前）。plugins 投影に無いキーは中核とみなす（checks.py）
DEFAULT_CORE_SERVICES = (
    "timeline", "ledger", "control", "intake", "manager", "inspector", "hq", "archiver", "gate",
    "resources", "memory-connector", "worker-pool", "emergency",
)
# 起動し直しの対象にしない物（自分自身。ホスト見張りは別に除く）
SELF_NAMES = ("observer",)


@dataclass
class Settings:
    host: str = "unknown"
    state_dir: str = "/state"
    data_dir: str = "/data"
    timeline_url: str = "http://timeline:8080"
    ledger_url: str = "http://ledger:8080"
    redact_index: str | None = "/state/redact/index"
    pii_list: str | None = None
    observe_s: float = 60
    stale_beats: int = 3
    heartbeat_s: float = 60
    lag_alert_s: float = 300
    starve_min: float = 60
    repeat_threshold: int = 5
    expired_grace_min: float = 15
    manager_silent_min: float = 30
    manager_bad_streak: int = 3
    unknown_unhandled_min: float = 30
    drain_silent_min: float = 10
    alert_dedup_min: float = 30
    core_services: tuple[str, ...] = DEFAULT_CORE_SERVICES
    commander_units: tuple[str, ...] = ()
    feed_backfill: int = 2000
    repeat_window_min: float = 60  # S-17 repeat_threshold は「1時間あたり」

    @classmethod
    def from_env(cls, env: dict | None = None, defaults=None) -> "Settings":
        e = os.environ if env is None else env
        s = cls()
        if defaults is None:
            try:
                from axcommon.defaults import Defaults

                defaults = Defaults.load()
            except Exception:  # noqa: BLE001  config が読めなくても動く（fail-soft。クラスの既定値で動く）
                defaults = None
        if defaults is not None:
            for name in ("observe_s", "stale_beats", "heartbeat_s", "lag_alert_s", "starve_min", "repeat_threshold",
                         "expired_grace_min", "manager_silent_min", "manager_bad_streak", "unknown_unhandled_min",
                         "drain_silent_min", "alert_dedup_min"):
                try:
                    setattr(s, name, defaults.get(name))
                except Exception:  # noqa: BLE001
                    pass
        s.host = e.get("AX_HOST", s.host)
        s.state_dir = e.get("AX_STATE_DIR", s.state_dir)
        s.data_dir = e.get("AX_OBSERVER_DATA", s.data_dir)
        s.timeline_url = e.get("AX_TIMELINE_URL") or s.timeline_url
        s.ledger_url = e.get("AX_LEDGER_URL") or s.ledger_url
        s.redact_index = e.get("AX_REDACT_INDEX", s.redact_index) or None
        s.pii_list = e.get("AX_PII_LIST") or None
        units = e.get("AX_OBSERVER_COMMANDER_UNITS", "")
        s.commander_units = tuple(u.strip() for u in units.split(",") if u.strip())
        return s

    @property
    def stale_s(self) -> float:
        """生存の判定の閾値。stale_beats 回分（S-11.2）の beat の間隔。"""
        return self.stale_beats * self.heartbeat_s

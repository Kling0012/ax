"""config/inspection.yaml の読み込み（S-13.1）と、S-17 の点検の値。範囲の外の値は範囲の中へ寄せ、寄せたことを記録する。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from axcommon.defaults import Defaults, config_dir


class InspectionConfigError(ValueError):
    pass


@dataclass
class InspectionConfig:
    initial_h: float = 24
    min_h: float = 24
    max_h: float = 168
    first_at: str | None = None
    clamped: bool = False
    # S-17
    quiet_count: int = 3
    stretch: float = 1.5
    notice_min: float = 30
    drain_wait_h: float = 2
    drain_max_h: float = 6
    state_dir: str = "/state/inspector"
    host: str = "unknown"
    control_token_name: str = "control-token-inspection"
    urls: dict = field(default_factory=dict)

    def clamp(self, hours: float) -> float:
        return max(self.min_h, min(self.max_h, float(hours)))

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, defaults: Defaults | None = None, environ: dict | None = None) -> "InspectionConfig":
        import yaml

        env = os.environ if environ is None else environ
        d = defaults or Defaults.load()
        p = Path(path) if path else config_dir() / "inspection.yaml"
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        errs = _schema_errors(doc)
        if errs:
            raise InspectionConfigError(f"{p.name}: {errs[0]}")
        iv = doc["interval_h"]
        if iv["min"] > iv["max"]:
            raise InspectionConfigError("interval_h: min が max より大きい")
        if doc.get("first_at") is not None:
            from axcommon.timeutil import parse_ts

            try:
                parse_ts(doc["first_at"])
            except ValueError:
                raise InspectionConfigError("first_at は RFC 3339 の日時にする") from None
        cfg = cls(initial_h=iv["initial"], min_h=iv["min"], max_h=iv["max"], first_at=doc.get("first_at"),
                  quiet_count=int(d.get("inspection_quiet_count")), stretch=float(d.get("inspection_stretch")),
                  notice_min=float(d.get("inspection_notice_min")), drain_wait_h=float(d.get("drain_wait_h")),
                  drain_max_h=float(d.get("drain_max_h")), host=env.get("AX_HOST", "unknown"),
                  state_dir=env.get("AX_INSPECTOR_STATE_DIR", "/state/inspector"))
        c = cfg.clamp(cfg.initial_h)
        if c != cfg.initial_h:
            cfg.initial_h, cfg.clamped = c, True
        from axcommon.client import service_url

        cfg.urls = {n: env.get(f"AX_{n.upper().replace('-', '_')}_URL") or service_url(n) for n in ("timeline", "control")}
        return cfg


def _schema_errors(doc) -> list[str]:
    from core.manager.extschemas import errors

    return errors("inspection-config", doc)

"""S-17 の既定値（config/defaults.yaml）の読み込みと範囲の検査。

使い方:
  d = Defaults.load()
  d.get("heartbeat_s")                      # 既定値
  d.get("heartbeat_s", override=20)         # 上書き。範囲外なら DefaultsError
  d.in_range("heartbeat_s", 5)              # False
範囲を持たない項目（「登録簿で検証」など）は範囲の検査をしない。
置き場: 環境変数 AX_CONFIG_DIR > /ax/config > リポジトリの config/。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class DefaultsError(ValueError):
    pass


def config_dir() -> Path:
    env = os.environ.get("AX_CONFIG_DIR")
    if env:
        return Path(env)
    for cand in (Path("/ax/config"), Path(__file__).resolve().parent.parent / "config"):
        if cand.is_dir():
            return cand
    raise DefaultsError("config/ が見つからない（AX_CONFIG_DIR を設定する）")


class Defaults:
    def __init__(self, table: dict[str, dict]):
        self._t = table

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Defaults":
        import yaml

        p = Path(path) if path else config_dir() / "defaults.yaml"
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("defaults"), dict):
            raise DefaultsError(f"defaults.yaml の形が違う: {p}")
        return cls(doc["defaults"])

    def names(self) -> list[str]:
        return sorted(self._t)

    def entry(self, name: str) -> dict:
        try:
            return self._t[name]
        except KeyError:
            raise DefaultsError(f"S-17 にない既定値: {name}") from None

    def range(self, name: str) -> tuple[Any, Any] | None:
        e = self.entry(name)
        if "min" in e and "max" in e:
            return e["min"], e["max"]
        return None

    def in_range(self, name: str, value: Any) -> bool:
        r = self.range(name)
        if r is None:
            return True
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return r[0] <= value <= r[1]

    def get(self, name: str, override: Any = None) -> Any:
        e = self.entry(name)
        if override is None:
            return e["default"]
        if not self.in_range(name, override):
            raise DefaultsError(f"{name}={override!r} は範囲 {self.range(name)} の外")
        return override

    def retry_wait_s(self, attempt: int) -> float:
        """再試行の待ち（S-17: 30秒 × 2^回数、最大 30分）。attempt は 0 から。"""
        b = self.entry("retry_backoff")["default"]
        return float(min(b["base_s"] * (b["factor"] ** attempt), b["max_s"]))

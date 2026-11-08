"""警報の型。(check, target) の組が重複除去の鍵（D-10.1）。"""
from __future__ import annotations

from dataclasses import dataclass

# 宛先（S-11.2 の表）。issued_by は制御命令の出した者の名前がそのまま入る（manager、backup、inspection、emergency、user など）
DEST_MANAGER = "manager"
DEST_BACKUP = "backup"
DEST_HOST = "host"
DEST_INSPECTION = "inspection"


@dataclass(frozen=True)
class Alert:
    check: str
    target: str
    destinations: tuple[str, ...]
    severity: str = "warn"
    detail: str = ""  # 短い説明。値や秘密を入れない

    activation_event: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.check, self.target)

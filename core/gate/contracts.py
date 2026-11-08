from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RelayEvidence:
    """gate が intake に渡す検査済みの事実。HTTP ヘッダの形は未決定。"""
    core_token: str
    device_id: str
    device: str
    bearer: str = ""


@dataclass(repr=False)
class RelayRequest:
    method: str
    path: str
    evidence: RelayEvidence
    declared: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    request_id: str = ""

    def __repr__(self):
        return f"RelayRequest({self.method!r}, {self.path.split('?')[0]!r}, credentials='redacted')"


@dataclass
class Reply:
    status: int
    data: Any
    headers: dict[str, str] = field(default_factory=dict)


async def unresolved_transport(request: RelayRequest, timeout: float) -> Reply:
    raise NotImplementedError("SPEC S-6.14 / DESIGN D-5.3: gate-to-intake credential wire is unspecified")

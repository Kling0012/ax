"""管理役が使う窓口（axcommon.client の薄い拡張）。資格は compose の secrets から読む。

- 制御 API: control-token-manager（issued_by=manager を決める専用の資格。S-5.3）。
- 台帳（中核の口 8080 の書き込み）: core-token-manager（core-tokens の表の名前 manager。F-18.4 の表へ足す名前。OPEN.md）。
- worker-pool の POST /v1/jobs: core-token-manager（中核の口。ax-core の足から呼ぶ。F-18.4）。
- 時系列記録 8080 の書き込みは資格を要しない（core/timeline の現行の実装）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from axcommon.client import ApiClient, ControlClient, LedgerClient, TimelineClient, WorkerPoolClient, Unavailable
from axcommon.creds import read_secret_optional


class ManagerControl(ControlClient):
    def commands(self, *, state: str | None = None, issued_by: str | None = None) -> list[dict]:
        r = self.get("/v1/commands", params={"state": state, "issued_by": issued_by}) or {}
        return list(r.get("commands") or [])

    def release(self, command_id: str, reason: str) -> Any:
        return self.post(f"/v1/commands/{command_id}/release", {"reason": reason})

    def renew(self, command_id: str, expires_at: str, reason: str) -> Any:
        return self.post(f"/v1/commands/{command_id}/renew", {"expires_at": expires_at, "reason": reason})

    def plugin(self, plugin_id: str) -> Any:
        return self.get(f"/v1/plugins/{plugin_id}")

    def create_timer(self, timer: dict) -> Any:
        return self.post("/v1/timers", timer)


class ManagerLedger(LedgerClient):
    def __init__(self, *args, writes_configured: bool = True, **kw):
        super().__init__(*args, **kw)
        self.writes_configured = writes_configured

    def request(self, method, path, **kw):
        if method.upper() not in ("GET", "HEAD") and not self.writes_configured:
            # Keep requests in our outbox until the missing credential contract is configured.
            raise Unavailable("not_implemented:manager_ledger_authority")
        return super().request(method, path, **kw)

    def todos(self, **params) -> list[dict]:
        r = self.list_todos(**params) or {}
        return list(r.get("todos") or [])


class ResourcesClient(ApiClient):
    def models(self) -> list[dict]:
        r = self.get("/v1/resources", params={"kind": "model"}) or {}
        items = r.get("resources") if isinstance(r, dict) else r
        return [x for x in (items or []) if isinstance(x, dict)]


class MemoryClient(ApiClient):
    def remember(self, body: dict) -> Any:
        return self.post("/v1/remember", body)


@dataclass
class Clients:
    timeline: Any
    control: Any
    ledger: Any
    pool: Any
    resources: Any = None
    memory: Any = None
    extra: dict = field(default_factory=dict)


def build_clients(cfg, *, transport=None, environ: dict | None = None) -> Clients:
    """本番の組み立て。pool の資格が無ければ送信前に失敗する。transport は試験用の差し替え。"""
    ctl_tok = read_secret_optional(cfg.control_token_name)
    core_tok = read_secret_optional(cfg.core_token_name)
    u = cfg.urls
    kw = {"transport": transport} if transport else {}
    return Clients(
        timeline=TimelineClient(u["timeline"], **kw),
        control=ManagerControl(u["control"], ctl_tok, **kw),
        ledger=ManagerLedger(u["ledger"], core_tok, writes_configured=bool(core_tok), **kw),
        pool=WorkerPoolClient(u["worker-pool"], token=core_tok, **kw),
        resources=ResourcesClient(u["resources"], **kw),
        memory=MemoryClient(u["memory-connector"], **kw),
    )

"""配備・復旧の共通型（S-20.3、S-20.6、S-25.6）。

この部品は環境を直接操作しない。製品登録簿で確定した要求を env.run
へ渡し、入口が返したバックアップ、転送、起動、healthz、復旧の結果を
失敗を隠さずに公開待ちへ返す。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class PermissionDecision:
    """実行直前の継続許可照合の結果。"""

    allowed: bool
    reason: str
    permission_id: str | None = None


@dataclass(frozen=True)
class MigrationStatus:
    """migration の自動配備可否を表す証拠。"""

    present: bool = False
    rollback_evidence: str = "not_applicable"
    reversible: bool = False
    individual_approval: bool = False

    def allows_deploy(self) -> bool:
        if not self.present:
            return True
        if self.individual_approval:
            return True
        return self.rollback_evidence == "verified" and self.reversible


@dataclass(frozen=True)
class DeploymentRequest:
    """製品登録簿が解決した配備依頼。"""

    project: str
    repo: str
    destination: str
    sha: str
    requires: tuple[str, ...]
    entry_name: str
    deployable: bool = True
    excluded_reason: str | None = None
    migration: MigrationStatus = MigrationStatus()


@dataclass(frozen=True)
class DeploymentResult:
    """公開待ちが記録する配備結果。"""

    outcome: str
    reason: str
    sha: str
    env_id: str | None
    stages: Mapping[str, str]
    recovery: Mapping[str, Any]
    card: Mapping[str, Any] | None


EnvRun = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class DeploymentService:
    """製品に依存しない配備前照合と入口結果の検証。"""

    _required_preserved = frozenset({".env", "secrets/", "backups/"})
    _failure_stages = ("transfer", "start", "healthz")

    def build_env_request(self, request: DeploymentRequest) -> dict[str, Any]:
        """S-25.3 の env.run 依頼を、許可済みの固定入口だけで組み立てる。"""

        return {
            "requires": list(request.requires),
            "sensitivity": "private",
            "entry": {
                "kind": "entrypoint",
                "name": request.entry_name,
                "args": {
                    "sha": request.sha,
                    "migration_approved": request.migration.individual_approval,
                },
            },
            "inputs": [{"ref": f"git:{request.repo}@{request.sha}"}],
            "outputs": ["result.json", "deploy.log"],
            "budget_min": 70,
            "stall_min": 15,
            "irreversible": True,
            "scope": "one",
            "idempotent": False,
            "idempotency_key": f"deploy:{request.repo}:{request.destination}:{request.sha}",
        }

    def deploy(
        self,
        request: DeploymentRequest,
        permission: PermissionDecision,
        env_run: EnvRun,
    ) -> DeploymentResult:
        """許可と migration を照合してから env.run を一度だけ呼ぶ。"""

        if not request.deployable:
            return self._blocked(request, request.excluded_reason or "deployment_excluded")
        if not request.requires or not request.entry_name:
            return self._blocked(request, "deployment_unregistered")
        if not permission.allowed:
            return self._blocked(request, f"permission_denied:{permission.reason}")
        if not request.migration.allows_deploy():
            return self._blocked(request, "migration_requires_approval")

        try:
            raw = env_run(self.build_env_request(request))
        except Exception as exc:
            return self._failed(request, "env_run_unavailable", {}, {"outcome": "unknown", "error": str(exc)})
        return self._interpret(request, raw)

    def _blocked(self, request: DeploymentRequest, reason: str) -> DeploymentResult:
        return DeploymentResult("blocked", reason, request.sha, None, {}, {}, None)

    def _interpret(self, request: DeploymentRequest, raw: Mapping[str, Any]) -> DeploymentResult:
        payload = raw.get("result", raw)
        if not isinstance(payload, Mapping):
            return self._failed(request, "invalid_entry_result", {}, {"outcome": "unknown"})
        stages = payload.get("stages", {})
        if not isinstance(stages, Mapping):
            return self._failed(request, "invalid_entry_result", {}, {"outcome": "unknown"})
        stage_view = {str(key): str(value) for key, value in stages.items()}
        env_id = raw.get("env_id") if isinstance(raw.get("env_id"), str) else None

        if stage_view.get("backup") != "ok":
            return self._failed(request, "backup_failed", stage_view, {"outcome": "not_needed"}, env_id)

        transfer = payload.get("transfer", {})
        if not isinstance(transfer, Mapping):
            return self._failed(request, "invalid_transfer_result", stage_view, {"outcome": "unknown"}, env_id)
        preserved = transfer.get("preserved", [])
        if transfer.get("uses_delete") is not False or not self._required_preserved.issubset(set(preserved)):
            return self._failed(request, "unsafe_transfer_result", stage_view, {"outcome": "unknown"}, env_id)

        failed_stage = next((name for name in self._failure_stages if stage_view.get(name) != "ok"), None)
        if failed_stage:
            recovery = self._recovery(payload)
            return self._failed(request, f"{failed_stage}_failed", stage_view, recovery, env_id)
        if raw.get("state") not in (None, "succeeded"):
            return self._failed(request, f"env_job_{raw.get('state', 'unknown')}", stage_view, self._recovery(payload), env_id)
        return DeploymentResult("succeeded", "ok", request.sha, env_id, stage_view, {"outcome": "not_needed"}, None)

    def _recovery(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = payload.get("recovery", {})
        recovery = dict(value) if isinstance(value, Mapping) else {}
        if recovery.get("db_rollback") is True:
            return {"outcome": "failed", "reason": "db_rollback_forbidden", "db_rollback": False}
        recovery.setdefault("outcome", "unknown")
        recovery["db_rollback"] = False
        return recovery

    def _failed(
        self,
        request: DeploymentRequest,
        reason: str,
        stages: Mapping[str, str],
        recovery: Mapping[str, Any],
        env_id: str | None = None,
    ) -> DeploymentResult:
        card = {
            "kind": "deployment_failure",
            "repo": request.repo,
            "destination": request.destination,
            "sha": request.sha,
            "reason": reason,
            "migration_present": request.migration.present,
            "db_recovery_required": request.migration.present,
            "recovery": dict(recovery),
        }
        return DeploymentResult("failed", reason, request.sha, env_id, dict(stages), dict(recovery), card)

from __future__ import annotations

from plugins.subloop.release.deploy import (
    DeploymentRequest,
    DeploymentService,
    MigrationStatus,
    PermissionDecision,
)
from tests.fakes.b15.deploy.parts import FakeEnvRun


SHA = "a" * 40


def request(**overrides):
    values = {
        "project": "hibi",
        "repo": "example-owner/hibi-server",
        "destination": "archive-host",
        "sha": SHA,
        "requires": ("deploy-target:hibi-server",),
        "entry_name": "hibi-server-deploy",
    }
    values.update(overrides)
    return DeploymentRequest(**values)


def successful_entry():
    return {
        "state": "succeeded",
        "env_id": "fake-archive-host",
        "result": {
            "stages": {"backup": "ok", "transfer": "ok", "start": "ok", "healthz": "ok"},
            "transfer": {"uses_delete": False, "preserved": [".env", "secrets/", "backups/"]},
        },
    }


def test_t32_checks_permission_before_env_run():
    runner = FakeEnvRun(successful_entry())
    result = DeploymentService().deploy(request(), PermissionDecision(False, "not_registered"), runner)

    assert result.outcome == "blocked"
    assert result.reason == "permission_denied:not_registered"
    assert runner.requests == []


def test_t35_backup_failure_does_not_continue_deploy():
    runner = FakeEnvRun({"env_id": "fake-archive-host", "result": {"stages": {"backup": "failed"}}})
    result = DeploymentService().deploy(request(), PermissionDecision(True, "ok"), runner)

    assert result.outcome == "failed"
    assert result.reason == "backup_failed"
    assert result.recovery["outcome"] == "not_needed"
    assert result.card["migration_present"] is False


def test_t35_failed_healthz_requires_known_non_db_recovery():
    entry = successful_entry()
    entry["result"]["stages"]["healthz"] = "failed"
    entry["result"]["recovery"] = {"outcome": "ok", "db_rollback": False}
    result = DeploymentService().deploy(request(), PermissionDecision(True, "ok"), FakeEnvRun(entry))

    assert result.outcome == "failed"
    assert result.reason == "healthz_failed"
    assert result.recovery == {"outcome": "ok", "db_rollback": False}
    assert result.card["db_recovery_required"] is False


def test_t35_refuses_unsafe_transfer_and_unverified_migration():
    unsafe = successful_entry()
    unsafe["result"]["transfer"]["uses_delete"] = True
    unsafe_result = DeploymentService().deploy(request(), PermissionDecision(True, "ok"), FakeEnvRun(unsafe))
    migration_runner = FakeEnvRun(successful_entry())
    migration_result = DeploymentService().deploy(
        request(migration=MigrationStatus(present=True, rollback_evidence="unknown")),
        PermissionDecision(True, "ok"),
        migration_runner,
    )

    assert unsafe_result.reason == "unsafe_transfer_result"
    assert migration_result.outcome == "blocked"
    assert migration_result.reason == "migration_requires_approval"
    assert migration_runner.requests == []


def test_t89_uses_deploy_capability_and_fixed_entrypoint():
    runner = FakeEnvRun(successful_entry())
    result = DeploymentService().deploy(request(), PermissionDecision(True, "ok"), runner)

    assert result.outcome == "succeeded"
    sent = runner.requests[0]
    assert sent["requires"] == ["deploy-target:hibi-server"]
    assert sent["entry"] == {
        "kind": "entrypoint",
        "name": "hibi-server-deploy",
        "args": {"sha": SHA, "migration_approved": False},
    }


def test_t95_registry_excludes_agent_memory_without_calling_env_run():
    runner = FakeEnvRun(successful_entry())
    result = DeploymentService().deploy(
        request(repo="example-owner/hibi-memory", deployable=False, excluded_reason="agent_memory_external"),
        PermissionDecision(True, "ok"),
        runner,
    )

    assert result.outcome == "blocked"
    assert result.reason == "agent_memory_external"
    assert runner.requests == []

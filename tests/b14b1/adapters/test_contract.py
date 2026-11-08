"""T-85, T-86, T-88, and T-116 for the common environment adapters."""
from __future__ import annotations

import pytest

from core.resources.envs.adapters import (
    ArtifactIntegrityError,
    ContainerAdapter,
    CredentialReferenceError,
    LocalAdapter,
    ResidentWorkloadError,
    SecretDetected,
    SSHHostAdapter,
    SubmissionUncertain,
)
from tests.fakes.b14b1.adapters import FakeEnvironmentExecutor


def _spec(job_id: str = "job-1", key: str = "idem-1", secrets: list[str] | None = None) -> dict:
    return {"job_id": job_id, "idempotency_key": key, "secrets": [] if secrets is None else secrets}


@pytest.fixture(params=[LocalAdapter, SSHHostAdapter, ContainerAdapter])
def adapter(request):
    executor = FakeEnvironmentExecutor()
    instance = request.param(
        executor=executor,
        remote_id="fake-env",
        connection_credential="env-ssh-fake",
        work_credentials=("registered-slot",),
    )
    return instance, executor


def test_t85_all_adapter_kinds_share_job_contract_and_verify_artifacts(adapter):
    instance, fake = adapter
    assert instance.submit(_spec()) == {"job_id": "job-1", "state": "queued"}
    fake.finish("job-1", logs=["completed"], artifacts={"result.json": b'{"ok":true}'})

    assert instance.status("job-1")["state"] == "succeeded"
    assert instance.logs("job-1") == ("completed",)
    artifact = instance.fetch("job-1")[0]
    assert (artifact.name, artifact.size, len(artifact.sha256)) == ("result.json", 11, 64)
    assert instance.cancel("job-1")["state"] == "cancelled"
    assert instance.probe()["reachable"] is True


def test_t85_disconnect_after_submit_is_reconciled_by_lookup_without_resubmit(adapter):
    instance, fake = adapter
    fake.disconnect_after.add("submit")
    with pytest.raises(SubmissionUncertain):
        instance.submit(_spec())

    assert instance.lookup("idem-1") == {"job_id": "job-1", "state": "queued"}
    assert [request.command.name for request in fake.requests].count("submit") == 1


def test_t85_rejects_artifact_with_wrong_content_hash(adapter):
    instance, fake = adapter
    instance.submit(_spec())
    fake.finish("job-1", artifacts={"result.json": b"ok"})
    fake.jobs["job-1"]["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ArtifactIntegrityError):
        instance.fetch("job-1")


def test_t86_only_registered_credential_references_cross_adapter_boundary(adapter):
    instance, fake = adapter
    with pytest.raises(CredentialReferenceError):
        instance.submit(_spec(secrets=["unregistered-slot"]))
    instance.submit(_spec(secrets=["registered-slot"]))
    request = fake.requests[-1]
    assert request.payload == _spec(secrets=["registered-slot"])
    assert request.connection.credential_ref == "env-ssh-fake"


def test_t86_scans_recovered_logs_and_artifacts(adapter):
    instance, fake = adapter
    instance.submit(_spec())
    fake.finish("job-1", logs=["safe", "needle"], artifacts={"result.json": b"clean"})
    with pytest.raises(SecretDetected):
        instance.collect("job-1", lambda text: "needle" in text)
    fake.finish("job-1", logs=["safe"], artifacts={"result.json": b"needle"})
    with pytest.raises(SecretDetected):
        instance.collect("job-1", lambda text: "needle" in text)


def test_t88_rental_releases_to_archive_only_after_complete_zero_cleanup(adapter):
    instance, fake = adapter
    instance.submit(_spec())
    fake.finish("job-1")
    assert instance.release_rental("job-1") == {"job_id": "job-1", "archived_to": "archive/job-1"}
    assert not hasattr(instance, "delete")
    assert not hasattr(instance, "destroy")


@pytest.mark.parametrize(
    "cleanup",
    [
        {"tracking": "unknown", "resident_processes": 0, "resident_containers": 0},
        {"tracking": "complete", "resident_processes": 1, "resident_containers": 0},
        {"tracking": "complete", "resident_processes": 0, "resident_containers": 1},
    ],
)
def test_t88_rental_refuses_residual_or_untracked_work(adapter, cleanup):
    instance, fake = adapter
    instance.submit(_spec())
    fake.finish("job-1", cleanup=cleanup)
    with pytest.raises(ResidentWorkloadError):
        instance.release_rental("job-1")
    assert "release" not in [request.command.name for request in fake.requests]


def test_t116_ssh_adapter_passes_only_credential_reference_and_fixed_subcommands():
    fake = FakeEnvironmentExecutor()
    instance = SSHHostAdapter(
        executor=fake,
        remote_id="example-local",
        connection_credential="env-ssh-example-local",
    )
    instance.probe()
    instance.submit(_spec())

    assert [request.command.argv for request in fake.requests] == [
        ("ax-env-run", "probe"),
        ("ax-env-run", "submit", "job-1"),
    ]
    assert all(request.connection.credential_ref == "env-ssh-example-local" for request in fake.requests)
    assert not hasattr(instance, "execute")
    assert not hasattr(instance, "run")

"""Adapters for the shared environment-job contract (S-25.3)."""

from .base import (
    AdapterError,
    Artifact,
    ArtifactIntegrityError,
    CollectedJob,
    CredentialReferenceError,
    EnvironmentAdapter,
    ResidentWorkloadError,
    SecretDetected,
    SubmissionUncertain,
)
from .container import ContainerAdapter
from .local import LocalAdapter
from .ssh_host import SSHHostAdapter

__all__ = [
    "AdapterError",
    "Artifact",
    "ArtifactIntegrityError",
    "CollectedJob",
    "ContainerAdapter",
    "CredentialReferenceError",
    "EnvironmentAdapter",
    "LocalAdapter",
    "ResidentWorkloadError",
    "SecretDetected",
    "SSHHostAdapter",
    "SubmissionUncertain",
]

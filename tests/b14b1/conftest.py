"""B14b-1 の台帳・選択試験の共通データ。"""
from __future__ import annotations

from copy import deepcopy

import pytest


@pytest.fixture
def environment():
    return {
        "id": "example-local",
        "adapter": "ssh-host",
        "connect": {"kind": "ssh", "host": "${AX_ENV_NODEA_HOST}"},
        "conn_credential": {"slot": "env-ssh-example-local", "place": "secrets_dir/env-ssh-example-local"},
        "os": "linux",
        "capabilities": ["ci-light", "security-audit"],
        "entrypoints": {"audit-readonly": {"run": "entrypoints/bin/audit-readonly"}},
        "data_classes": ["public", "private", "sensitive"],
        "secrets_allowed": False,
        "work_credentials": [],
        "secret_targets": [],
        "max_concurrent": 1,
        "cost": {"kind": "fixed"},
        "lifetime": "permanent",
        "owner_use": False,
        "rank": 100,
        "registered_at": "2026-11-07",
        "state": "available",
        "day_use_s": 0,
    }


@pytest.fixture
def environment_copy(environment):
    return lambda **changes: {**deepcopy(environment), **changes}

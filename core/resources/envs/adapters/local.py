"""The local adapter uses the same injected runner contract as remote hosts."""
from __future__ import annotations

from .base import BaseEnvironmentAdapter


class LocalAdapter(BaseEnvironmentAdapter):
    adapter_kind = "local"

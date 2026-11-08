"""Container adapter; container transport remains replaceable and offline-testable."""
from __future__ import annotations

from .base import BaseEnvironmentAdapter


class ContainerAdapter(BaseEnvironmentAdapter):
    adapter_kind = "container"

"""Hibi release asset checks. The publishing entry remains an environment concern."""
from __future__ import annotations


def assets_ready(assets):
    if not isinstance(assets, list):
        return False
    names = [a.get('name', '') if isinstance(a, dict) else str(a) for a in assets]
    return (any(name.endswith('.dmg') for name in names)
            and any(name.endswith('setup.exe') for name in names)
            and any(name.startswith('latest') and name.endswith('.yml') for name in names))

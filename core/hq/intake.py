"""Shared HQ-only intake client, preserving the b10a transport contract."""
from .notify.intake import DEFAULT_SOCKET, IntakeClient, IntakeError, unix_transport

__all__ = ["DEFAULT_SOCKET", "IntakeClient", "IntakeError", "unix_transport"]

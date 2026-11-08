"""Safe, replaceable invocation primitives for environment adapters."""

from .execution import Command, Connection, ConnectionLost, Executor

__all__ = ["Command", "Connection", "ConnectionLost", "Executor"]

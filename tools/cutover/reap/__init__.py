"""B17 operator-only reap lifecycle (S-14.3, F-20, D-22.2).

The B17 common executor supplies Ports; there is no control API or timer action.
Importing this package never starts a service or executes a command.
"""
from .operator import Outcome, Paths, Ports, ReapOperator

__all__ = ["Outcome", "Paths", "Ports", "ReapOperator"]

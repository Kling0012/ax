"""S-19 integration boundary; external services are injected."""
from .gate import Decision, MergeGate
from .flow import ChangeFlow
from .journal import Journal, Learning
from .ports import DependencyUnavailable, Ports

__all__ = ["ChangeFlow", "Decision", "DependencyUnavailable", "MergeGate", "Ports", "Journal", "Learning"]

"""Operator-side retirement entry point; callers supply all effects."""
from .engine import (
    CommandResult, Context, Host, PRECONDITIONS, Profile, RetirementResult,
    StepFailure, SYSTEM_GROUPS, USER_GROUPS, X_GROUPS, run_retirement, stop_set,
)

__all__ = ["CommandResult", "Context", "Host", "PRECONDITIONS", "Profile",
           "RetirementResult", "StepFailure", "SYSTEM_GROUPS", "USER_GROUPS",
           "X_GROUPS", "run_retirement", "stop_set"]

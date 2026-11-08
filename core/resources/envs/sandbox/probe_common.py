"""Provider-local trial state and independent gate probe results."""
from dataclasses import dataclass
import math


@dataclass
class ProviderState:
    env_id: str
    state: str = "trial"
    gate_state: str = "trial"
    gate_state_reason: str | None = None
    gate_key: str = "never"
    gate_checked_at: float | None = None
    ax_probe_ok: bool = False

    def worker_key_unavailable(self):
        self.gate_key = "missing"
        self.gate_checked_at = None
        self.gate_state = "excluded"
        self.gate_state_reason = "worker_key_unavailable"

    def record_ax_probe(self, ok: bool):
        self.ax_probe_ok = ok is True

    def complete_j_trial(self, *, success: bool, trial_for: str):
        if self.state != "trial" or not self.ax_probe_ok or trial_for != self.env_id or success is not True:
            raise ValueError("invalid_j_trial")
        self.state = "available"

    def complete_g_trial(self, *, success: bool, trial_for: str, now: float, max_age_s: float):
        if (self.state != "available" or self.gate_state != "trial" or trial_for != self.env_id
                or success is not True or not gate_ready(self, now, max_age_s)):
            raise ValueError("invalid_g_trial")
        self.gate_state = "available"


def apply_gate_key(env: ProviderState, value: str, now: float):
    reasons = {"missing": "credential_missing", "insecure": "credential_insecure",
               "invalid": "credential_invalid", "auth_failed": "auth_failed",
               "unreachable": "unavailable"}
    if value not in {*reasons, "ok"} or not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
        raise ValueError("invalid_gate_probe")
    env.gate_key = value
    env.gate_checked_at = now
    if value in reasons:
        env.gate_state = "excluded"
        env.gate_state_reason = reasons[value]
        if value == "unreachable":
            env.state = "excluded"


def gate_ready(env: ProviderState, now: float, max_age_s: float) -> bool:
    try:
        return (env.gate_key == "ok" and env.gate_checked_at is not None
                and math.isfinite(now) and math.isfinite(max_age_s)
                and max_age_s >= 0 and 0 <= now - env.gate_checked_at <= max_age_s)
    except (TypeError, ValueError):
        return False

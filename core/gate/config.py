from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str
    bind_addr: str
    state_dir: Path
    seats_file: Path
    core_token_file: Path
    allowed_users_file: Path
    config_dir: Path
    port: int = 8909
    gate_body_max_kb: int = 64
    ax_output_max_kib: int = 256
    gate_read_per_s: int = 10
    gate_write_per_min: int = 30
    gate_inflight_max: int = 8
    gate_call_timeout_s: float = 30
    read_relay_timeout_s: float = 5
    approval_wait_max_s: float = 20
    gate_enroll_fail_max: int = 5
    gate_enroll_lock_min: int = 15
    intake_host: str = "intake"
    intake_port: int = 8082

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"{self.host}:{self.port}".lower(), f"{self.bind_addr}:{self.port}"}

    @property
    def approval_wait(self) -> float:
        return max(0, min(self.approval_wait_max_s, self.gate_call_timeout_s - 5))

    @classmethod
    def from_env(cls, env=None):
        import os
        env = os.environ if env is None else env
        return cls(host=env["AX_HOST"], bind_addr=env["AX_BIND_ADDR"],
                   state_dir=Path(env.get("AX_GATE_STATE_DIR", "/run/gate")),
                   seats_file=Path(env.get("AX_SEATS_FILE", "/run/seat/seats.yaml")),
                   core_token_file=Path(env.get("AX_GATE_CORE_TOKEN_FILE", "/run/secrets/core-token-gate")),
                   allowed_users_file=Path(env.get("AX_GATE_ALLOWED_USERS_FILE", "/run/secrets/hq-allowed-users")),
                   config_dir=Path(env.get("AX_CONFIG_DIR", "/ax/config")),
                   port=int(env.get("AX_GATE_PORT", "8909")),
                   intake_host=env.get("AX_GATE_INTAKE_HOST", "intake"),
                   intake_port=int(env.get("AX_GATE_INTAKE_PORT", "8082")))

"""ホスト見張りの設定。profile（S-16.3）と環境変数から作る。実在のホスト名・アドレスの値はコードに持たない。

環境変数（試験・運用の差し替え）:
  AX_PROFILE            明示した profile の場所（AX_PROFILE_FILE と既定値より優先）
  AX_PROFILE_FILE       profile の場所（既定 ~/.config/ax/profile/default.yaml）
  AX_HOME               本番の clone（既定 ~/.local/share/ax/prod）
  AX_HOSTWATCH_SUDO     特権の前置き（既定 "sudo -n"）
  AX_CONFIG_DIR         S-17 の config/defaults.yaml の場所（既定はリポジトリの config/）
  AX_HOSTWATCH_REPO     host/ のあるリポジトリの根（既定はこのファイルから辿る）
"""
from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


@dataclass
class HostConfig:
    state_dir: Path
    ax_home: Path
    host: str = "unknown"
    commander_units: tuple[str, ...] = ()
    sudo: tuple[str, ...] = ("sudo", "-n")
    profile_path: Path = Path("~/.config/ax/profile/default.yaml")
    repo: Path = REPO
    observer_stale_s: float = 180
    host_watch_s: float = 60
    index_rebuild_s: float = 3600
    max_requests_per_cycle: int = 50
    deploy_timeout_s: float = 3600
    compose_timeout_s: float = 600
    up_wait_s: int = 120

    @property
    def project_args(self) -> list[str]:
        install_env = self.ax_home / "config" / "launcher.env"
        optional = ["--env-file", str(install_env)] if install_env.is_file() else []
        return ["-p", "ax", "--project-directory", str(self.ax_home), *optional,
                "--env-file", str(self.state_dir / "deploy" / "profile.env"),
                "--env-file", str(self.state_dir / "deploy" / "current.env")]

    def compose_files(self) -> list[str]:
        args = ["-f", str(self.ax_home / "compose.yaml")]
        d = self.ax_home / "compose.d"
        if d.is_dir():
            for f in sorted(d.glob("*.yaml")):
                args += ["-f", str(f)]
        return args

    def compose(self, *tail: str, profiles: tuple[str, ...] = ()) -> list[str]:
        """sudo -n docker compose [--profile P] -p ax … -f compose.yaml -f compose.d/*.yaml <tail>"""
        pf: list[str] = []
        for p in profiles:
            pf += ["--profile", p]
        return [sys.executable, "-P", str(self.repo / "host/lib/hostwatch/ipam_compose.py"),
                *self.sudo, "docker", "compose", *pf, *self.project_args, *self.compose_files(), "--", *tail]

    @property
    def log_path(self) -> Path:
        return self.state_dir / "hostwatch.log"

    @property
    def registered_dir(self) -> Path:
        return self.state_dir / "hostwatch" / "registered"

    @property
    def requests_dir(self) -> Path:
        return self.state_dir / "host-requests"

    def is_commander(self, name: str) -> bool:
        stem = name[:-8] if name.endswith(".service") else name
        return stem == "paseo" or stem.startswith("paseo-") or any(
            stem == (u[:-8] if u.endswith(".service") else u) for u in self.commander_units
        )


def load(env: dict | None = None) -> HostConfig:
    e = os.environ if env is None else env
    from axcommon import profile as prof

    ppath = Path(os.path.expanduser(e.get("AX_PROFILE") or e.get("AX_PROFILE_FILE") or "~/.config/ax/profile/default.yaml"))
    raw = prof._load_yaml(ppath)
    penv = prof.build_env(raw, environ={"AX_HOME": e.get("AX_HOME", "")})
    units = raw.get("commander_unit") or ()
    if isinstance(units, str):
        units = (units,)
    cfg = HostConfig(state_dir=Path(penv["AX_STATE_DIR"]), ax_home=Path(penv["AX_HOME"]), host=penv["AX_HOST"],
                     commander_units=tuple(str(u) for u in units), sudo=tuple(shlex.split(e.get("AX_HOSTWATCH_SUDO") or "sudo -n")),
                     profile_path=ppath, repo=Path(e.get("AX_HOSTWATCH_REPO") or REPO))
    try:
        from axcommon.defaults import Defaults

        d = Defaults.load()
        cfg.observer_stale_s = float(d.get("observer_stale_s"))
        cfg.host_watch_s = float(d.get("host_watch_s"))
    except Exception:  # noqa: BLE001  config が読めなくても既定値で動く（fail-soft）
        pass
    return cfg

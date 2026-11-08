"""tests/b8 のホスト側の補助（HostConfig、ps の出力、hostwatch.log の読み取り）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "host" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from hostwatch.config import HostConfig  # noqa: E402
from hostwatch.hostlog import HostLog  # noqa: E402
from hostwatch.runner import Result  # noqa: E402


def make_cfg(tmp: Path, **kw) -> HostConfig:
    state = tmp / "state"
    home = tmp / "axhome"
    for d in (state / "hostwatch" / "registered", state / "host-requests", state / "deploy", home / "compose.d"):
        d.mkdir(parents=True, exist_ok=True)
    (home / "compose.yaml").write_text("name: ax\n")
    (home / "compose.d" / "b1.yaml").write_text("services: {}\n")
    (home / "compose.d" / "b8.yaml").write_text("services: {}\n")
    (state / "deploy" / "profile.env").write_text("AX_TAG=x\n")
    (state / "deploy" / "current.env").write_text("AX_TAG=oldtag000000\n")
    cfg = HostConfig(state_dir=state, ax_home=home, host="test-host", commander_units=("paseo-test.service",),
                     profile_path=tmp / "profile.yaml", repo=ROOT, **kw)
    return cfg


def register(cfg: HostConfig, *names: str) -> None:
    for n in names:
        (cfg.registered_dir / n).touch()


def ps_out(rows: dict[str, tuple[str, str]], ndjson: bool = True) -> str:
    items = [{"Service": s, "State": st, "Health": h, "Name": f"ax-{s}-1"} for s, (st, h) in rows.items()]
    return "\n".join(json.dumps(i) for i in items) if ndjson else json.dumps(items)


def services_out(*names: str) -> str:
    return "\n".join(names) + "\n"


def log_events(cfg: HostConfig) -> list[dict]:
    p = cfg.log_path
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def quiet_log(cfg: HostConfig) -> HostLog:
    import io

    return HostLog(cfg.log_path, cfg.host, stream=io.StringIO())


def write_request(cfg: HostConfig, body: dict | str, name: str = "01HZZ0000000000000000000A1.req") -> Path:
    p = cfg.requests_dir / name
    p.write_text(body if isinstance(body, str) else json.dumps(body))
    return p

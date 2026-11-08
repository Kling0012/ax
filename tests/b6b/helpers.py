"""tests/b6b の補助。launcher（ax_launcher.py）を偽の docker・偽の sudo で動かす砂場。実際の docker・sudo・ネットワークは使わない。"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from tests.fakes.b6b.fake_docker import FakeDocker

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_PY = ROOT / "host" / "env-module-min" / "releases" / "b6" / "lib" / "ax_launcher.py"
ENV_RUN = ROOT / "host" / "env-module-min" / "releases" / "b6" / "bin" / "ax-env-run"
FAKE_DOCKER = ROOT / "tests" / "fakes" / "b6b" / "fake_docker.py"
FAKE_SUDO = ROOT / "tests" / "fakes" / "b6b" / "fake_sudo.py"
TOKEN = "tok-FAKE-" + "0123456789abcdef"  # 試験用のダミー（秘密ではない）


def load_launcher():
    spec = importlib.util.spec_from_file_location("ax_launcher_under_test", LAUNCHER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ax_launcher_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class Sandbox:
    def __init__(self, tmp: Path, ro_mounts: list | None = None):
        self.tmp = tmp
        self.home = tmp / "home"
        self.ax_home = tmp / "axhome"
        self.state = tmp / "state"
        self.hddtmp = tmp / "hddtmp"
        self.secrets = tmp / "secrets"
        self.work = tmp / "work"
        self.env_home = tmp / "envhome"
        self.gamedata = tmp / "gamedata"
        for d in (self.home, self.state / "deploy", self.hddtmp, self.secrets, self.work, self.env_home):
            d.mkdir(parents=True, exist_ok=True)
        (self.ax_home / "config").mkdir(parents=True)
        shutil.copytree(ROOT / "config" / "runners", self.ax_home / "config" / "runners")
        (self.secrets / "model-gateway-key").write_text(TOKEN + "\n")
        (self.state / "deploy" / "current.env").write_text("AX_TAG=test\n")
        self.gamedata.mkdir()
        ro = ro_mounts if ro_mounts is not None else [
            {"id": "sod-gamedata", "host_path": str(self.gamedata), "container_path": "/mnt/sod-gamedata", "sensitivity": "private"}]
        (self.ax_home / "config" / "ro-mounts.yaml").write_text(yaml.safe_dump(ro))
        self.profile = tmp / "profile.yaml"
        self.profile.write_text(yaml.safe_dump({
            "secrets_dir": str(self.secrets), "tmp_dir": str(self.hddtmp), "ax_state_dir": str(self.state), "sandbox_gate_port": 8931}))
        self.fd = FakeDocker(tmp / "fakedocker")
        self.sudo_log = tmp / "sudo.log"
        self.env = {
            "HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "AX_ENV_HOME": str(self.env_home), "AX_HOME": str(self.ax_home), "AX_PROFILE": str(self.profile), "AX_WORK_DIR": str(self.work),
            "AX_ENV_SUDO": f"{sys.executable} {FAKE_SUDO} -n", "AX_ENV_DOCKER": f"{sys.executable} {FAKE_DOCKER}",
            "FAKE_SUDO_LOG": str(self.sudo_log), **self.fd.env(),
        }
        self.mod = load_launcher()

    # --- 呼び出し ---
    def call(self, sub: str, *args: str, stdin: dict | str | None = None, caller: str = "launcher", extra_env: dict | None = None):
        env = dict(self.env)
        env.update(extra_env or {})
        env["SSH_ORIGINAL_COMMAND"] = " ".join([sub, *args]).strip()
        if sub == "job-start" and isinstance(stdin, dict):
            stdin = {"prompt": "test prompt", **stdin}
        text = stdin if isinstance(stdin, str) else json.dumps(stdin or {})
        return self.mod.dispatch(["--caller", caller], env, lambda size=None: text if size is None else text[:size])

    def start(self, job_id: str = "j1", *, runner: str = "agent-cli", model: str | None = "zhipu/test-model", **fields):
        req = {"job_id": job_id, "runner": runner, "allow_hosts": ["api.example.org"], "budget_min": 30}
        if model is not None and runner != "verify-node":
            req["model"] = model
        req.update(fields)
        return self.call("job-start", job_id, stdin=req)

    def run_args(self, job_id: str) -> list[str]:
        c = self.fd.container(f"ax-job-{job_id}")
        assert c is not None, "コンテナが無い"
        return c["run_argv"]

    def run_calls(self) -> list[list[str]]:
        return [c["argv"] for c in self.fd.calls() if c["argv"] and c["argv"][0] == "run"]

    def chain_dir(self, root: str) -> Path:
        return self.hddtmp / f"job-{root}"

    def work_dir(self, wd: str) -> Path:
        return self.work / f"job-{wd}"

    def subprocess_env_run(self, original_command: str | None, *argv: str, stdin: str = "", extra_env: dict | None = None):
        env = dict(self.env)
        env.update(extra_env or {})
        if original_command is not None:
            env["SSH_ORIGINAL_COMMAND"] = original_command
        return subprocess.run([str(ENV_RUN), *argv], input=stdin, capture_output=True, text=True, env=env)


def flag_values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]

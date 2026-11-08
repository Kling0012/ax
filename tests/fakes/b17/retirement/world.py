"""Three filesystem-backed hosts, fake systemctl/docker/ax; never start services."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

from tools.cutover.retirement import (
    CommandResult, Context, Host, PRECONDITIONS, Profile, USER_GROUPS,
)
from tools.cutover.retirement import fsops


class World:
    def __init__(self, root: Path):
        self.root = root
        self.calls = []
        self.events = []
        self.cards = {}
        self.t = 0.0
        self.states = {}
        self.missing = set()
        self.busy_until = {}
        self.held = set()
        self.unreachable = set()
        self.fail_disable = set()
        self.fail_umount = False
        self.cross_device = set()
        self.fail_move = set()
        self.go = True
        self.file_reads = []
        self.renewals = 0
        self.h = {}
        for role in ("local", "legacy", "compute-host"):
            base = root / role
            home = base / "home/operator"
            host = Host(role + "-fake", str(home), str(home / ".config/systemd/user"),
                        str(base / "etc/systemd/system"), str(base / "usr/local/sbin"),
                        str(base / "etc/fstab"), str(base / "mnt/ghauto-sbx/sod-gamedata"),
                        str(base / "run/user/1000"), 1000, str(base / "disk") if role == "compute-host" else None)
            self.h[role] = host
            Path(host.user_units).mkdir(parents=True)
            Path(host.system_units).mkdir(parents=True)
            Path(host.sbin).mkdir(parents=True)
            Path(host.runtime_dir).mkdir(parents=True)
            Path(host.fstab).write_text("/source /mnt/ghauto-sbx/sod-gamedata none bind 0 0\n")
            if role == "local":
                for group in (*USER_GROUPS, "archive-to-hdd", "magpie-gateway", "agent-memory", "paseo"):
                    self.unit(host, "user", group + ".service", active=group in ("magpie-gateway", "agent-memory", "paseo"))
                    self.unit(host, "user", group + ".timer", active=True)
            elif role == "legacy":
                self.unit(host, "user", "issue-automation.service")
                self.unit(host, "user", "issue-automation.timer", active=False, enabled=False)
                self.unit(host, "user", "archive-to-hdd.service", active=False)
                self.unit(host, "user", "archive-to-hdd.timer", active=True)
            else:
                self.unit(host, "user", "issue-automation-compute-host-prune.service")
                self.unit(host, "user", "issue-automation-compute-host-prune.timer", active=True)
            if role != "compute-host":
                for unit in ("issue-automation-sbx-nft.service", "issue-automation-sbx-nft.timer", "websbx-nft.service"):
                    self.unit(host, "system", unit, active=True)
                for name in ("issue-automation", "issue-automation-compute-host-runner", "issue-automation-compute-host-setup", "issue-automation-remote-test", "issue-automation.bak-old"):
                    self.file(home / ".claude/bin" / name, "script\n")
                for name in ("issue-automation-sbx-nft-refresh", "websbx-nft-apply"):
                    self.file(Path(host.sbin) / name, "script\n")
                for directory in (".claude/issue-automation/locks", ".claude/work/issue-automation", ".cache/issue-automation"):
                    self.file(home / directory / "entry", "state-data")
            else:
                for directory in ("issue-automation-compute-host/bin", "issue-automation-compute-host/repos", "issue-automation-compute-host/data/sod-gamedata"):
                    self.file(home / directory / "entry", "payload")
                (home / "issue-automation-compute-host/jobs").mkdir()
                for directory in ("issue-automation-compute-host/nuget", "issue-automation-compute-host/archive", "issue-automation-compute-host/test-results"):
                    self.file(Path(host.disk_root) / directory / "entry", "disk-payload")
            self.file(Path(host.user_units) / ("issue-automation-compute-host-prune.service.d/override.conf" if role == "compute-host" else "issue-automation.service.d/override.conf"), "override")
            self.file(Path(host.user_units) / ("issue-automation-compute-host-prune.timer.bak-old" if role == "compute-host" else "issue-automation.timer.bak-old"), "backup")
        self.profile = Profile(self.h["local"], self.h["legacy"], self.h["compute-host"], "20300102",
                               {key: True for key in PRECONDITIONS}, wait_seconds=3, poll_seconds=1)
        self.test_result("T-78", "ok")
        self.test_result("T-142", "ok")

    @staticmethod
    def file(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def unit(self, host, scope, name, active=False, enabled=None):
        directory = host.user_units if scope == "user" else host.system_units
        self.file(Path(directory) / name, "[Unit]\nDescription=fake\n")
        if enabled is None:
            enabled = name.endswith(".timer") or active
        self.states[host.name, scope, name] = ["active" if active else "inactive", "enabled" if enabled else "static" if name.endswith(".service") else "disabled"]

    def test_result(self, subject, result):
        self.record("cutover.state", subject, {"result": result})

    def latest(self, subject):
        return next((e for e in reversed(self.events) if e["subject"] == subject), None)

    def record(self, kind, subject, data):
        self.t += 0.001
        self.events.append({"kind": kind, "subject": subject, "data": data, "at": self.t})

    def card(self, key, title, body):
        self.cards[key] = (title, body)

    def authorize(self):
        self.renewals += 1
        if not self.go:
            raise PermissionError("fake-ax-claim-or-go-rejected")

    def sleep(self, seconds):
        self.t += seconds

    def context(self):
        return Context(self.execute, self.record, self.latest, self.card, self.authorize,
                       lambda host: self.latest('T-142')['data']['result'] == 'ok',
                       lambda: self.t, self.sleep, lambda: f"2030-01-02T00:00:{self.t:09.6f}+00:00")

    def execute(self, host, argv, stdin=None):
        argv = tuple(argv)
        self.t += .001
        self.calls.append((self.t, host, argv, stdin))
        if host in self.unreachable:
            return CommandResult(255)
        h = next(h for h in self.h.values() if h.name == host)
        file_argv = argv[2:] if argv[:2] == ("sudo", "-n") else argv
        archive_env = {}
        if len(file_argv) >= 2 and file_argv[0] == "env" and file_argv[1].startswith("AX_RETIREMENT_ARCHIVE_NAME="):
            archive_env["AX_RETIREMENT_ARCHIVE_NAME"] = file_argv[1].split("=", 1)[1]
            file_argv = file_argv[2:]
        if file_argv[:3] == ("python3", "-I", "-c"):
            data = json.loads(stdin)
            op = data["op"]
            if op == "busy" and (host in self.held or self.busy_until.get((host, "locks"), 0) > self.t):
                return CommandResult(0, json.dumps({"busy": True, "items": ["fake-held-lock"]}))
            if op in ("preflight", "move") and host in self.cross_device:
                return CommandResult(1, "", "fake-EXDEV")
            if op == "move" and host in self.fail_move:
                return CommandResult(1)
            try:
                self.file_reads.extend(data.get("lock_patterns", []))
                with patch.dict("os.environ", archive_env):
                    return CommandResult(0, json.dumps(fsops.handle(data)))
            except (ValueError, OSError) as exc:
                return CommandResult(1, "", type(exc).__name__)
        if "systemctl" in argv:
            i = argv.index("systemctl") + 1
            scope = "user" if argv[i:i+1] == ("--user",) else "system"
            if scope == "user":
                i += 1
            verb, args = argv[i], argv[i+1:]
            if verb == "disable":
                for unit in args:
                    if unit.startswith("--"):
                        continue
                    key = (host, scope, unit)
                    if key in self.fail_disable:
                        continue
                    self.states[key] = ["inactive", "disabled"]
                return CommandResult()
            if verb in ("is-active", "is-enabled"):
                key = host, scope, args[0]
                state = self.states.get(key, ["inactive", "not-found"])
                if verb == "is-active" and self.busy_until.get((host, args[0]), 0) > self.t:
                    return CommandResult(0, "active\n")
                value = state[0 if verb == "is-active" else 1]
                return CommandResult(0 if value in ("enabled", "active", "static") else 3, value + "\n")
            if verb in ("list-units", "list-timers"):
                directory = Path(h.user_units if scope == "user" else h.system_units)
                names = [p.name for p in directory.iterdir() if p.suffix in (".service", ".timer")]
                if verb == "list-timers":
                    names = [n for n in names if n.endswith(".timer") and self.states.get((host, scope, n), ["inactive"])[0] == "active"]
                return CommandResult(0, "\n".join(names))
            if verb == "show":
                directory = Path(h.user_units if scope == "user" else h.system_units)
                return CommandResult(0, "loaded\n" if (directory / args[0]).exists() else "not-found\n")
            if verb in ("daemon-reload", "reset-failed"):
                return CommandResult()
            raise AssertionError(argv)
        if argv[:2] == ("ss", "-ltn"):
            return CommandResult(0, "State Recv-Q Send-Q Local Address:Port Peer Address:Port\n")
        if argv[:2] == ("ps", "-eo"):
            return CommandResult(0, "python fake-process\n")
        if argv[:2] == ("docker", "ps"):
            return CommandResult(0, "" if "--filter" in argv else "container-one retained\n")
        if argv[:4] == ("sudo", "-n", "nft", "list"):
            return CommandResult(0, "table inet ghauto_sbx {}\ntable inet websbx {}\n")
        if argv[:3] == ("sudo", "-n", "umount"):
            return CommandResult(32 if self.fail_umount else 0)
        if argv[:3] == ("sudo", "-n", "findmnt"):
            return CommandResult()
        if argv[0] == "git":
            return CommandResult()  # Fixture scripts are intentionally not tracked.
        raise AssertionError(f"Unexpected executable: {argv[0]}")

    def fileops(self, op=None):
        return [(at, host, json.loads(stdin)) for at, host, argv, stdin in self.calls
                if stdin is not None and (op is None or json.loads(stdin)["op"] == op)]

    def stops(self):
        return [(at, host, argv) for at, host, argv, _ in self.calls if "disable" in argv]

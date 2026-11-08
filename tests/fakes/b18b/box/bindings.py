"""Fixture-only b18a bindings. Never loaded by production resources."""
from __future__ import annotations

import copy
import json
import shlex
from pathlib import PurePosixPath

from core.resources.box.box_mcp import BoxBindings, CAP_TOOLS, CAP_STAGE1_BASIS, RUNNER_VERSION
from core.resources.box.protocol import BoxError, ToolDenied

AX_KEY = "fixture-ax-key-0000000001"
WORKER_KEY = "fixture-worker-key-000001"
ACTIONS = {"click", "double_click", "right_click", "triple_click", "type", "key", "hotkey", "scroll", "drag", "launch_app", "wait", "locate"}


class FakePolicy:
    """Test oracle for the S-25.19 shared policy boundary, not a second runtime policy."""

    def __init__(self):
        self.key = AX_KEY
        self.key_error = None
        self.key_calls = []
        self.check_calls = []
        self.scan_calls = []
        self.blocked = "FIXTURE_SECRET_SENTINEL"
        self.action_allow = set(ACTIONS)

    def read_key(self, provider, purpose):
        self.key_calls.append((provider, purpose))
        if (provider, purpose) != ("box", "ax"):
            raise BoxError("credential_purpose_mismatch")
        if self.key_error:
            raise BoxError(self.key_error)
        return self.key

    def check_tool_args(self, name, args):
        self.check_calls.append(name)
        if not isinstance(args, dict):
            raise ToolDenied("arg_rule:" + name)
        if name != "computer_act":
            return
        denied = lambda: ToolDenied("arg_rule:computer_act")
        if "type_from_file" in self.action_allow:
            raise denied()
        if "action" not in args and "actions" not in args:
            raise denied()
        targets = [args]
        if "actions" in args:
            if not isinstance(args["actions"], list) or len(args["actions"]) > 50:
                raise denied()
            targets += args["actions"]
        for target in targets:
            if not isinstance(target, dict) or {"field", "path"} & target.keys():
                raise denied()
            if "action" in target and (not isinstance(target["action"], str) or target["action"] not in self.action_allow):
                raise denied()

    def scan_outbound(self, value):
        self.scan_calls.append("outbound")
        encoded = value if isinstance(value, str) else json.dumps(value)
        if self.blocked in encoded:
            return False
        if any(word in encoded for word in ("box connect", "@simular-ai/box", "api.instabox.sh", "mcp.box.simular.ai")):
            raise BoxError("forbidden_command")
        return True

    def scan_inbound(self, value):
        self.scan_calls.append("inbound")
        return self.blocked.encode() not in value


class FakeFreeGuard:
    """Synthetic decoded fields; real provider decoding belongs to b18a/measurement."""

    @staticmethod
    def identify(env, listing):
        if not isinstance(listing, dict) or not isinstance(listing.get("boxes"), list):
            raise BoxError("free_unverified")
        boxes = listing["boxes"]
        if not boxes:
            raise BoxError("no_machine")
        if len(boxes) != 1:
            raise BoxError("machines_over_limit")
        machine = boxes[0]
        if not isinstance(machine, dict) or not isinstance(machine.get("name"), str) or not isinstance(machine.get("id"), str):
            raise BoxError("free_unverified")
        profile = env.get("profile", {})
        name, ident = profile.get("box_machine_name", "main"), profile.get("box_machine_id", "")
        if (name and machine["name"] != name) or (ident and machine["id"] != ident) or (ident and not name):
            raise BoxError("free_unverified")
        return machine

    def check(self, env, listing, usage, billing, previous):
        result = {"free_ok": False, "machines": None}
        if isinstance(listing, dict) and isinstance(listing.get("boxes"), list):
            result["machines"] = len(listing["boxes"])
        try:
            machine = self.identify(env, listing)
            result.update(machine_name=machine["name"], machine_id=machine["id"])
            if not isinstance(usage, dict):
                raise BoxError("free_unverified")
            today, month = usage.get("today", {}).get("cost_micros"), usage.get("month", {}).get("cost_micros")
            if any(type(value) is not int or value < 0 for value in (today, month)):
                raise BoxError("free_unverified")
            result.update(cost_micros_today=today, cost_micros_month=month)
            if today > 0 or month > 0:
                raise BoxError("cost_nonzero")
            if billing == {"billing_not_enabled": True}:
                result.update(billing_not_enabled=True, free_ok=True)
                return result
            if not isinstance(billing, dict) or billing.get("plan_enabled") is not True:
                raise BoxError("free_unverified")
            included = billing.get("included_boxes")
            wallet = billing.get("wallet_balance")
            if not isinstance(included, list) or type(wallet) is not int or wallet < 0:
                raise BoxError("free_unverified")
            if machine["name"] not in included:
                raise BoxError("not_free")
            previous_wallet = previous.get("wallet_balance")
            if previous_wallet is not None and wallet < previous_wallet:
                raise BoxError("cost_nonzero")
            result.update(wallet_balance=wallet, billing_not_enabled=False, free_ok=True)
        except BoxError as error:
            result["reason"] = error.code
        except (AttributeError, TypeError):
            result["reason"] = "free_unverified"
        return result


class FakeRunnerClient:
    def __init__(self, fake):
        self.fake = fake
        self.selfcheck_paths = []

    def selfcheck_command(self, env):
        command = 'cd / && env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$HOME" sh /workspace/ax/bin/ax-sbx-run selfcheck'
        credentials = [item for item in env.get("work_credentials", []) if item.get("slot") == "scheduled-publisher-identity"]
        if credentials:
            place = credentials[0]["place"]
            # place is the documented scheme:path; this is a synthetic runner ABI.
            path = place.split(":", 1)[-1]
            cli = str(PurePosixPath(env.get("runner", {}).get("root", "/workspace/ax")).parent / "bin/scheduled-publisher")
            self.selfcheck_paths.append((cli, path))
            command += " " + shlex.quote(cli) + " " + shlex.quote(path)
        return command

    def install_runner(self, driver, env):
        if self.fake.install_failures:
            self.fake.install_failures -= 1
            raise BoxError("tool_error")
        self.fake.runner_version = "ax-sbx 1"
        return {"runner": "ok"}

    def submit(self, driver, env, job):
        driver.ensure_running(env)
        previous = driver.lookup(env, job["idempotency_key"])
        if isinstance(previous, dict) and previous.get("state") != "not_found":
            return previous
        ident = job["job_id"]
        self.fake.jobs[ident] = {"state": "running", "launched": True, "alive": True, "idempotency_key": job["idempotency_key"]}
        return dict(self.fake.jobs[ident], job_id=ident)

    def get_file(self, driver, env, path, offset, length):
        return self.fake._file(path).read_bytes()[offset:offset + length]


class FakeProbeCommon:
    """Reference oracle for b18a's common judgment, not production code."""

    def merge(self, env, tools, previous, measured):
        checked = dict(previous.get("capabilities_checked", {}))
        stage2 = dict(previous.get("stage2_checked", {}))
        for cap in CAP_TOOLS:
            checked.setdefault(cap, "unverified")
            if tools is not None:
                if not CAP_TOOLS[cap] <= tools:
                    checked[cap] = "unverified"
                elif CAP_STAGE1_BASIS[cap] and cap not in stage2:
                    checked[cap] = "ok"
        result = {"capabilities_checked": checked, "stage2_checked": stage2}
        if measured is None:
            return result
        def ok(value):
            return value == "ok" or (isinstance(value, dict) and value.get("status") == "ok")
        matrix = {"linux-sandbox": ("sh",), "web-screenshot-full": ("chromium", "node", "playwright", "fullpage"),
                  "scheduled-publisher-cli": ("scheduled-publisher",), "docker": ("docker",)}
        checks = measured["checks"]
        for cap, items in matrix.items():
            passed = CAP_TOOLS[cap] <= tools and all(ok(checks.get(item)) for item in items)
            if cap == "scheduled-publisher-cli":
                passed = passed and any(item.get("slot") == "scheduled-publisher-identity" for item in env.get("work_credentials", []))
            checked[cap] = stage2[cap] = "ok" if passed else "unverified"
        for cap, item in (("web-screenshot", "screenshot"), ("sandbox-desktop", "desktop"), ("web-gui", "gui_snapshot")):
            if measured[item] is not None:
                checked[cap] = stage2[cap] = "ok" if measured[item] and CAP_TOOLS[cap] <= tools else "unverified"
        result.update(runner_version=measured["runner_version"],
                      runner="ok" if measured["runner_version"] == RUNNER_VERSION else "missing",
                      gui_checked_at=measured["gui_checked_at"], disk_free_mib=measured["disk_free_mib"])
        checked["ax-runner"] = "ok" if result["runner"] == "ok" and env.get("detached_survival_verified") is True else "unverified"
        return result


def bindings(fake):
    return BoxBindings(FakePolicy(), FakeFreeGuard(), FakeRunnerClient(fake), FakeProbeCommon())

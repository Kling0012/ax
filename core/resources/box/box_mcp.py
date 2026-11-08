"""Box path J driver (S-25.31, D-23.3), installed at the PLAN.json path.

Shared policy, free guard and runner client are supplied by b18a. No key file
reader, gate backend, ledger, lease manager or service is duplicated here.
"""
from __future__ import annotations

import base64
import contextlib
import json
import re
import shlex
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .protocol import BoxError, IntegrationRequired, MCPSession, ToolDenied, tool_payload

CAP_TOOLS = {
    "linux-sandbox": frozenset({"shell", "read_file", "write_file"}),
    "web-screenshot": frozenset({"browser_navigate", "screenshot"}),
    "web-screenshot-full": frozenset({"shell"}),
    "web-gui": frozenset({"browser_navigate", "browser_click", "browser_type", "browser_snapshot"}),
    "sandbox-desktop": frozenset({"computer_act", "computer_state", "gui_plan"}),
    "scheduled-publisher-cli": frozenset({"shell"}),
    "docker": frozenset({"shell"}),
}
CAP_STAGE1_BASIS = {cap: cap not in {"web-screenshot-full", "scheduled-publisher-cli", "docker"} for cap in CAP_TOOLS}
PROBE_CALLS = frozenset({"initialize", "notifications/initialized", "tools/list", "box_status", "box_list", "usage", "billing"})
PROBE_IN_BOX_CALLS = frozenset({"shell", "screenshot", "computer_state", "browser_navigate", "browser_snapshot"})
DENIED_TOOLS = frozenset({"box_create", "box_delete", "box_upgrade", "box_ensure", "box_save", "box_restore_paths", "box_snapshots"})
ALLOWED_TOOLS = frozenset({
    "shell", "read_file", "write_file", "edit_file", "list_dir", "browser_navigate", "browser_fetch",
    "browser_read", "browser_snapshot", "browser_tabs", "browser_click", "browser_type", "browser_fill",
    "screenshot", "computer_state", "computer_act", "gui_plan", "desk", "script_run", "box_status",
    "box_list", "usage", "box_view", "box_wake", "box_hibernate", "billing",
})
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
RUNNER_VERSION = "ax-sbx 1"
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")


@dataclass(frozen=True)
class BoxBindings:
    """Explicit assembly boundary; callbacks do not receive credentials as arguments.

    policy implements read_key(provider, purpose), check_tool_args(name, args),
    scan_outbound(object), scan_inbound(bytes). A false scan result denies sending.
    guard implements identify(env, box_list), check(env, listing, usage, billing,
    previous) -> {free_ok, reason?, machines, machine_name?, machine_id?,
    cost_micros_today, cost_micros_month, billing_not_enabled, wallet_balance?}.
    runner is the shared b18a runner client. prepare_spec(env, job) normalizes the
    common job into the runner wire format; selfcheck_command(env) supplies the
    fixed invocation with the declared scheduled-publisher paths. Other methods are delegated.
    """

    policy: Any
    guard: Any
    runner: Any = None
    probe_common: Any = None


def _png(result: dict) -> bytes | None:
    images = result.get("content", [])
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("image"), dict):
        images = [structured["image"]] + images
    for item in images if isinstance(images, list) else []:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        try:
            value = base64.b64decode(item.get("data", ""), validate=True)
            if len(value) >= 33 and value[:8] == PNG_MAGIC and value[12:16] == b"IHDR":
                width, height = struct.unpack("!II", value[16:24])
                if width >= 1024 and width * 5 == height * 8:
                    return value
        except (ValueError, TypeError, struct.error):
            pass
    return None


class BoxMCPDriver:
    """One driver instance per environment; constructor performs no I/O.

    A host URL is supplied through the environment profile, never a real address
    embedded in code. Missing assembly dependencies disable this feature only.
    """

    def __init__(self, env: dict, *, transport: Callable, bindings: BoxBindings | None = None,
                 emit: Callable | None = None, clock: Callable = time.time,
                 sleep: Callable = time.sleep, allowed_tools=None):
        self.env, self.bindings, self.emit, self.clock, self.sleep = env, bindings, emit, clock, sleep
        self.allowed = frozenset(ALLOWED_TOOLS if allowed_tools is None else allowed_tools)
        self.enabled = (self.allowed <= ALLOWED_TOOLS and not bool(self.allowed & DENIED_TOOLS)
                        and all(names <= self.allowed for names in CAP_TOOLS.values()))
        self._tools: dict[str, dict] | None = None
        self._last_listing = None
        self._probe_lock = threading.RLock()
        self._mode = threading.local()
        self._previous: dict = dict(env.get("probe", {}))
        self._cards: set[str] = set()
        self._machine_awake_at: float | None = None
        self._last_usage_time: float | None = None
        self._awake_sample_s = 0.0
        url = env.get("mcp_url", "")
        self.session = MCPSession(url, transport, self._read_ax_key,
                                  timeout_s=self.limit("box_call_timeout_s", 120, "call_timeout_s"),
                                  clock=clock, emit=self._emit, env_id=env.get("id", "box"))

    def _emit(self, event: str, payload: dict):
        if self.emit is not None:
            # An event sink outage must not cause a remote operation to repeat.
            try:
                self.emit(event, payload)
            except Exception:
                pass

    def _need_bindings(self) -> BoxBindings:
        if self.bindings is None:
            raise IntegrationRequired("b18a BoxBindings required (policy, free_guard, runner_client)")
        return self.bindings

    def _read_ax_key(self):
        return self._need_bindings().policy.read_key("box", "ax")

    def limit(self, name: str, default: float, env_name: str | None = None):
        limits = self.env.get("limits", {})
        return limits.get(env_name or name, self.env.get(name, default))

    def _enabled(self):
        if not self.enabled:
            raise BoxError("driver_disabled")

    def open_session(self, env: dict | None = None):
        self._same_env(env)
        self._enabled()
        return self.session.request("initialize")

    def _same_env(self, env):
        if env is not None and env is not self.env and env != self.env:
            raise BoxError("environment_mismatch")

    @contextlib.contextmanager
    def _scope(self, name):
        previous = getattr(self._mode, "value", None)
        self._mode.value = name
        try:
            yield
        finally:
            self._mode.value = previous

    @contextlib.contextmanager
    def _job_scope(self, ident):
        previous = self.session.job_id
        self.session.job_id = ident
        try:
            yield
        finally:
            self.session.job_id = previous

    def _denied(self, name, args, why):
        self._emit("sandbox.call", {"job_id": self.session.job_id, "path": "J", "env_id": self.env.get("id", "box"),
                                   "tool": name, "args_bytes": len(json.dumps(args, default=lambda _: "").encode()),
                                   "response_bytes": 0, "duration_s": 0, "result": "tool_denied", "deny_reason": why})
        raise ToolDenied(why)

    def _path(self, path: str):
        root = PurePosixPath("/workspace/ax")
        if not isinstance(path, str) or "\x00" in path or "\\" in path:
            raise ToolDenied("path_condition")
        p = PurePosixPath(path)
        if not p.is_absolute() or ".." in p.parts or p == root or not p.is_relative_to(root):
            raise ToolDenied("path_condition")
        return str(p)

    def call_tool(self, name: str, args: dict, timeout_s: float | None = None) -> dict:
        self._enabled()
        mode = getattr(self._mode, "value", None)
        if name in DENIED_TOOLS or name not in self.allowed:
            self._denied(name, args, "not_in_table")
        if mode == "probe" and name not in PROBE_CALLS:
            self._denied(name, args, "not_in_table")
        if mode == "probe_in_box" and name not in PROBE_IN_BOX_CALLS:
            self._denied(name, args, "not_in_table")
        if name == "billing" and (mode != "probe" or args != {}):
            self._denied(name, args, "not_in_table")
        if name == "box_wake" and mode != "ensure_running":
            self._denied(name, args, "not_in_table")
        if name == "box_hibernate" and mode != "hibernate":
            self._denied(name, args, "not_in_table")
        if name == "box_view" and mode != "card_view":
            self._denied(name, args, "not_in_table")
        if self._tools is not None and name not in self._tools:
            self._denied(name, args, "not_in_table")
        policy = self._need_bindings().policy
        try:
            policy.check_tool_args(name, args)
        except Exception as error:
            if getattr(error, "code", None) == "tool_denied":
                self._denied(name, args, getattr(error, "deny_reason", "arg_rule:" + name))
            raise
        if not isinstance(args, dict):
            self._denied(name, args, "arg_rule:" + name)
        if name in {"write_file", "edit_file"}:
            try:
                self._path(args.get("path"))
            except ToolDenied:
                self._denied(name, args, "path_condition")
        if name == "browser_navigate":
            u = urlsplit(args.get("url", ""))
            if u.username is not None or getattr(u, "pass" + "word") is not None:
                raise BoxError("scan_blocked")
        if policy.scan_outbound(args) is False:
            raise BoxError("scan_blocked")
        bound = self.session.timeout_s
        timeout = bound if timeout_s is None else timeout_s
        if name in {"gui_plan", "script_run"}:
            timeout = min(timeout, 110)
        if not 0 < timeout <= bound:
            raise BoxError("timeout_out_of_range")
        result = self.session.request("tools/call", {"name": name, "arguments": args}, timeout_s=timeout)
        if result.get("isError"):
            # Exact documented exception; remote body is never an error message.
            if name == "billing" and tool_payload(result) == "box plans are not enabled":
                return {"billing_not_enabled": True}
            payload = tool_payload(result)
            if name == "write_file" and isinstance(payload, dict) and payload.get("code") == "disk_full":
                raise BoxError("disk_full")
            raise BoxError("tool_error")
        return result

    def list_tools(self):
        self._enabled()
        result = self.session.request("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list) or any(not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in tools):
            raise BoxError("invalid_response")
        self._tools = {t["name"]: t for t in tools}
        return self._tools

    def _machine_args(self, tool: str, *, wait=False) -> dict:
        """Derive the machine selector from tools/list, not an unmeasured name."""
        if self._tools is None:
            self.list_tools()
        definition = self._tools.get(tool, {})
        schema = definition.get("inputSchema", {})
        props = schema.get("properties", {})
        selectors = [key for key in ("box", "name", "box_name") if key in props]
        if len(selectors) != 1 or (wait and "wait" not in props):
            raise IntegrationRequired(f"SPEC S-25.31.11: {tool} inputSchema not measured")
        guard = self._need_bindings().guard
        machine = guard.identify(self.env, self._last_listing)
        args = {selectors[0]: machine["name"]}
        if wait:
            args["wait"] = True
        required = set(schema.get("required", []))
        if not required <= args.keys():
            raise IntegrationRequired(f"SPEC S-25.31.11: {tool} required arguments not measured")
        return args

    def machine_state(self):
        if self._last_listing is None:
            self._last_listing = tool_payload(self.call_tool("box_list", {}))
        return tool_payload(self.call_tool("box_status", self._machine_args("box_status")))

    def ensure_running(self, env: dict | None = None):
        self._same_env(env)
        with self._probe_lock, self._scope("ensure_running"):
            self.open_session()
            if self._tools is None:
                self.list_tools()
            self._last_listing = tool_payload(self.call_tool("box_list", {}))
            self._need_bindings().guard.identify(self.env, self._last_listing)
            state = self.machine_state()
            if isinstance(state, dict) and state.get("state") == "running":
                self._machine_awake_at = self.clock()
                return state
            if not isinstance(state, dict) or state.get("state") != "hibernated":
                raise BoxError("not_ready")
            self.call_tool("box_wake", self._machine_args("box_wake", wait=True))
            deadline = self.clock() + self.limit("box_wake_wait_s", 120, "ready_wait_s")
            while self.clock() <= deadline:
                state = self.machine_state()
                if isinstance(state, dict) and state.get("state") == "running":
                    self._machine_awake_at = self.clock()
                    self._emit("env.machine_clock", {"env_id": self.env.get("id", "box"), "awake_at": self.clock()})
                    return state
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                self.sleep(min(3, remaining))
            raise BoxError("not_ready")

    def wake(self):
        return self.ensure_running()

    def probe(self, env: dict | None = None) -> dict:
        self._same_env(env)
        with self._probe_lock, self._scope("probe"):
            self.session.job_id = "probe"
            previous = self._previous
            result = {"contract": self.env.get("contract", 1), "capabilities_checked": dict(previous.get("capabilities_checked", {})),
                      "runner": previous.get("runner", "unknown"), "machines": None, "machine_state": "unknown",
                      "cost_micros_today": None, "cost_micros_month": None, "tools_seen": 0, "disk_free_mib": None,
                      "image": "", "upgrade_available": False, "checked_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                      "prev_machine_state": previous.get("machine_state", "unknown"), "free_ok": False}
            for cap in CAP_TOOLS:
                result["capabilities_checked"].setdefault(cap, "unverified")
            try:
                self.open_session()
                tools_available = True
                try:
                    self.list_tools()
                    result["tools_seen"] = len(self._tools)
                except BoxError as error:
                    if error.code not in {"tool_error", "invalid_response"}:
                        raise
                    tools_available = False
                if tools_available and not {"box_list", "usage", "billing"} <= self._tools.keys():
                    raise BoxError("free_unverified")
                common = self._need_bindings().probe_common
                if common is None:
                    raise IntegrationRequired("b18a probe_common.merge required")
                result.update(common.merge(self.env, set(self._tools) if tools_available else None, previous, None))
                listing = tool_payload(self.call_tool("box_list", {}))
                self._last_listing = listing
                usage = tool_payload(self.call_tool("usage", {}))
                try:
                    billing_result = self.call_tool("billing", {})
                    billing = billing_result if billing_result.get("billing_not_enabled") else tool_payload(billing_result)
                except BoxError as error:
                    if error.code != "tool_error":
                        raise
                    billing = None
                free = self._need_bindings().guard.check(self.env, listing, usage, billing, previous)
                # Guard output is an explicit small set; no raw provider payload enters state/events.
                for key in ("machines", "machine_name", "machine_id", "cost_micros_today", "cost_micros_month", "billing_not_enabled", "wallet_balance", "free_ok"):
                    if key in free:
                        result[key] = free[key]
                if not free.get("free_ok"):
                    raise BoxError(free.get("reason", "free_unverified"))
                try:
                    state = self.machine_state()
                    if isinstance(state, dict):
                        result["machine_state"] = state.get("state") if state.get("state") in {"running", "hibernated"} else "unknown"
                        result["image"] = state.get("image", "")
                        result["upgrade_available"] = state.get("upgrade_available") is True
                        result["last_saved_at_ms"] = state.get("last_saved_at_ms")
                except (BoxError, IntegrationRequired) as error:
                    if isinstance(error, BoxError) and error.code in {"auth_failed", "quota", "unavailable", "timed_out", "rate_limited"}:
                        raise
                result["stage2_checked"] = dict(previous.get("stage2_checked", {}))
                result["gui_checked_at"] = previous.get("gui_checked_at")
                if result["machine_state"] == "running" and tools_available:
                    self.probe_in_box(result)
                self._usage_event(result)
                self._previous = result
                return result
            except BoxError as error:
                result["error"] = error.code
                result["failure"] = self.failure(error.code, retry_after=error.retry_after)
                self._usage_event(result)
                self._previous = result
                return result

    @staticmethod
    def _probe_error(error):
        if error.code in {"auth_failed", "quota", "unavailable", "timed_out", "rate_limited", "session_expired"}:
            raise error

    def probe_in_box(self, result: dict):
        with self._scope("probe_in_box"):
            tools = set(self._tools or {})
            checks: dict[str, Any] = {}
            version = ""
            measurements = {"screenshot": False, "desktop": False, "gui_snapshot": None,
                            "gui_checked_at": result.get("gui_checked_at"), "disk_free_mib": None}
            if "shell" in tools:
                try:
                    version = self._shell_text(self.call_tool("shell", {"command": self._runner_command("version")}, min(30, self.session.timeout_s)))
                except BoxError as error:
                    self._probe_error(error)
                try:
                    runner = self._need_bindings().runner
                    if runner is None:
                        raise IntegrationRequired("b18a runner_client.selfcheck_command required")
                    command = runner.selfcheck_command(self.env)
                    prefix = self._runner_command("selfcheck")
                    if not isinstance(command, str) or not command.startswith(prefix):
                        raise ToolDenied("arg_rule:runner")
                    suffix = shlex.split(command[len(prefix):])
                    for path in suffix:
                        value = PurePosixPath(path)
                        if not value.is_absolute() or ".." in value.parts or not value.is_relative_to("/workspace") or any(c in path for c in "\n\r\x00"):
                            raise ToolDenied("arg_rule:runner")
                    # Each suffix item must be individually quoted; shell operators
                    # outside quotes may not escape the fixed selfcheck invocation.
                    if command != prefix + (" " + shlex.join(suffix) if suffix else ""):
                        raise ToolDenied("arg_rule:runner")
                    payload = tool_payload(self.call_tool("shell", {"command": command}, min(30, self.session.timeout_s)))
                    if isinstance(payload, dict) and "stdout" in payload:
                        payload = json.loads(payload["stdout"])
                    if isinstance(payload, dict):
                        checks = payload.get("checks", payload)
                        disk = payload.get("disk", {})
                        if isinstance(disk, dict) and isinstance(disk.get("workspace_free_mib"), (int, float)):
                            measurements["disk_free_mib"] = disk["workspace_free_mib"]
                except BoxError as error:
                    self._probe_error(error)
                except (ValueError, TypeError):
                    pass
            measurements["runner_version"] = version.strip()
            measurements["checks"] = checks
            for cap, name in (("web-screenshot", "screenshot"), ("sandbox-desktop", "computer_state")):
                ok = False
                if CAP_TOOLS[cap] <= tools:
                    try:
                        response = self.call_tool(name, {})
                        ok = _png(response) is not None
                        if name == "computer_state":
                            payload = tool_payload(response)
                            ok = ok and isinstance(payload, dict) and isinstance(payload.get("windows"), list)
                    except BoxError as error:
                        self._probe_error(error)
                measurements["screenshot" if name == "screenshot" else "desktop"] = ok
            gui_due = (result["prev_machine_state"] == "hibernated" or result.get("gui_checked_at") is None or
                       self.clock() - result["gui_checked_at"] >= self.limit("sbx_gui_check_h", 24) * 3600 or bool(self.env.get("trial_for")))
            if CAP_TOOLS["web-gui"] <= tools and gui_due:
                ok = False
                restore = True
                try:
                    self.call_tool("browser_navigate", {"url": "https://example.com"})
                    payload = tool_payload(self.call_tool("browser_snapshot", {}))
                    if isinstance(payload, dict):
                        items = payload.get("elements", [])
                        ok = isinstance(items, list) and any(isinstance(item, dict) and item.get("ref") for item in items)
                except BoxError as error:
                    if error.code in {"auth_failed", "quota", "unavailable", "timed_out", "rate_limited", "session_expired"}:
                        restore = False
                        raise
                finally:
                    if restore:
                        with contextlib.suppress(BoxError):
                            self.call_tool("browser_navigate", {"url": "about:blank"})
                measurements["gui_checked_at"] = self.clock()
                measurements["gui_snapshot"] = ok
            # Only the shared b18a probe_common decides capability states.
            result.update(self._need_bindings().probe_common.merge(self.env, tools, result, measurements))
            if result["disk_free_mib"] is not None and result["disk_free_mib"] < self.limit("sbx_disk_min_gib", 1) * 1024:
                result["failure"] = self.failure("disk_full")

    def _usage_event(self, result):
        now = self.clock()
        if result["machine_state"] == "running" and self._previous.get("machine_state") == "running" and self._last_usage_time is not None:
            self._awake_sample_s += max(0, now - self._last_usage_time)
        self._last_usage_time = now
        payload = {key: result.get(key) for key in ("machines", "machine_state", "free_ok", "billing_not_enabled", "capabilities_checked")}
        payload.update({"env_id": self.env.get("id", "box"), "max_machines": 1, "snapshots": None, "stored_gib": None,
                        "free_sandbox_hours": None, "cost_usd": None if result["cost_micros_month"] is None else result["cost_micros_month"] / 1_000_000,
                        "ready": result["machine_state"] == "running", "auto_suspend_s": None, "max_run_min": None,
                        "queue_len": self.env.get("queue_len", 0), "awake_sample_s": self._awake_sample_s})
        self._emit("env.usage", payload)

    def usage(self, env: dict | None = None):
        self._same_env(env)
        return {"machines": tool_payload(self.call_tool("box_list", {})), "usage": tool_payload(self.call_tool("usage", {}))}

    usage_read = usage

    def failure(self, code: str, *, irreversible=False, launched=False, retry_after=None) -> dict:
        """Return a descriptor; the shared adapter/ledger applies it atomically."""
        permanent = {"credential_missing", "credential_invalid", "credential_insecure", "auth_failed", "quota", "no_machine",
                     "cost_nonzero", "machines_over_limit", "free_unverified", "not_free"}
        temporary = {"unavailable", "timed_out", "not_ready"}
        degraded = {"runner_install_failed", "disk_full", "artifact_scan_blocked"}
        rejected = {"credential_missing", "credential_invalid", "credential_insecure", "tool_denied", "sensitivity_denied", "repo_not_public", "scan_blocked", "forbidden_command", "no_machine"}
        job = "rejected" if code in rejected else "failed"
        if code == "timed_out":
            job = "timed_out"
        if code == "rate_limited":
            job = "queued"
        if irreversible and launched and code in {"machine_stopped", "session_reset", "unavailable", "timed_out"}:
            job = "lost"
        state = "excluded" if code in permanent | temporary else "degraded" if code in degraded else None
        card = code in permanent | degraded or job == "lost"
        token = f"{self.env.get('id', 'box')}:{code}"
        first_card = card and token not in self._cards
        if card:
            self._cards.add(token)
        return {"reason": code, "job_state": job, "env_state": state,
                "exclude_min": self.limit("env_exclude_min", 30) if code in temporary else None,
                "scope": "J" if code.startswith("credential_") or code == "auth_failed" else "provider",
                "card": first_card, "parked_card": False,
                "retry_after": (60 if retry_after is None else retry_after) if code == "rate_limited" else None}

    def _runner_command(self, sub: str, *args: str) -> str:
        if sub not in {"version", "selfcheck", "start", "status", "logs", "fetch", "cancel", "lookup", "busy", "archive-old", "mark"}:
            raise ToolDenied("arg_rule:runner")
        if any(not isinstance(arg, str) or not _IDENT.fullmatch(arg) for arg in args):
            raise ToolDenied("arg_rule:runner")
        return 'cd / && env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$HOME" sh /workspace/ax/bin/ax-sbx-run ' + " ".join([sub, *args])

    @staticmethod
    def _shell_text(result):
        payload = tool_payload(result)
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("stdout"), str):
            if payload.get("truncated"):
                raise BoxError("truncated")
            if payload.get("rc", 0) != 0:
                raise BoxError("tool_error")
            return payload["stdout"]
        raise BoxError("invalid_response")

    def sh(self, env: dict | None, sub: str, ident: str | None = None, timeout_s=30, *args):
        self._same_env(env)
        argv = (() if ident is None else (ident,)) + tuple(str(a) for a in args)
        job_ident = ident if sub in {"start", "status", "logs", "fetch", "cancel"} and ident is not None else self.session.job_id
        with self._job_scope(job_ident):
            response = self.call_tool("shell", {"command": self._runner_command(sub, *argv)}, min(timeout_s, 30, self.session.timeout_s))
        payload = tool_payload(response)
        # Some MCP responses expose JSON stdout directly as structuredContent.
        if isinstance(payload, dict) and ("stdout" not in payload or "since" in payload):
            return payload
        text = self._shell_text(response)
        try:
            return json.loads(text)
        except ValueError:
            return text

    def put_file(self, env: dict | None, path: str, text: str):
        self._same_env(env)
        self._path(path)
        if self._need_bindings().policy.scan_outbound(text) is False:
            raise BoxError("scan_blocked")
        try:
            return self.call_tool("write_file", {"path": path, "content": text})
        except BoxError as error:
            if error.code != "tool_error":
                raise
        # Each attempt gets a fresh staging file; never append to a partial target.
        staging = path + ".staging-" + str(int(self.clock() * 1_000_000))
        directory = str(PurePosixPath(path).parent)
        self.call_tool("shell", {"command": f"mkdir -p {shlex.quote(directory)} && : > {shlex.quote(staging)}"}, min(30, self.session.timeout_s))
        raw = text.encode()
        for offset in range(0, len(raw), 12288):
            chunk = base64.b64encode(raw[offset:offset + 12288]).decode()
            self.call_tool("shell", {"command": f"printf %s '{chunk}' | base64 -d >> {shlex.quote(staging)}"}, min(30, self.session.timeout_s))
        # Archiving an existing target belongs to runner_client before put_file.
        self.call_tool("shell", {"command": f"mv {shlex.quote(staging)} {shlex.quote(path)}"}, min(30, self.session.timeout_s))
        return {"written": len(raw)}

    def call_short(self, argv: list[str], timeout_s=30, stdin=None):
        if stdin is not None:
            raise IntegrationRequired("SPEC S-25.22: shell stdin wire shape not measured")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ToolDenied("arg_rule:shell")
        return self.call_tool("shell", {"command": shlex.join(argv)}, min(30, timeout_s, self.session.timeout_s))

    def _runner(self):
        runner = self._need_bindings().runner
        if runner is None:
            raise IntegrationRequired("b18a runner_client required")
        return runner

    def install_runner(self, env: dict | None = None):
        self._same_env(env)
        for _ in range(3):
            try:
                return self._runner().install_runner(self, self.env)
            except BoxError as error:
                if error.code not in {"tool_error", "invalid_response", "truncated"}:
                    raise
        raise BoxError("runner_install_failed")

    def submit(self, env: dict, job: dict):
        self._same_env(env)
        if job.get("sensitivity") != "public":
            raise BoxError("sensitivity_denied")
        kind = job.get("entry", {}).get("kind")
        if kind not in {"entrypoint", "command", "steps"}:
            raise BoxError("entry_unsupported")
        if job.get("irreversible") and kind != "entrypoint":
            raise BoxError("irreversible_not_allowed")
        # The shared adapter validates entry registration, inputs, free_command,
        # irreversible_ok, secrets, budgets, lease and idempotency before this call.
        with self._job_scope(job.get("job_id", "unknown")):
            return self._runner().submit(self, self.env, job)

    def status(self, env: dict, job_id: str, *, irreversible=False):
        self._same_env(env)
        try:
            actual = self.sh(env, "status", job_id)
            if not isinstance(actual, dict):
                raise BoxError("invalid_response")
            if actual.get("launched") and actual.get("rc") is None:
                machine = self.machine_state()
                if isinstance(machine, dict) and machine.get("state") not in {"running", None}:
                    return {"state": "lost" if irreversible else "failed", "reason": "machine_stopped"}
            return actual
        except BoxError as error:
            if error.code not in {"timed_out", "tool_error", "unavailable"}:
                raise
            # Never turn a shell reset into an immediate terminal result.
            actual = self.sh(env, "status", job_id)
            if not isinstance(actual, dict):
                raise BoxError("invalid_response")
            if actual.get("rc") is not None or actual.get("alive") is True:
                return actual
            if actual.get("launched"):
                return {"state": "lost" if irreversible else "failed", "reason": "session_reset"}
            return actual

    def logs(self, env: dict, job_id: str, since=0):
        return self.sh(env, "logs", job_id, 30, str(since))

    def fetch(self, env: dict, job_id: str, name: str, offset=0, length=12288):
        self._same_env(env)
        if not isinstance(offset, int) or offset < 0 or not isinstance(length, int) or not 0 < length <= 12288:
            raise ToolDenied("arg_rule:runner")
        while length:
            try:
                result = self.sh(env, "fetch", job_id, 30, name, str(offset), str(length))
                encoded = result.get("data") if isinstance(result, dict) else result
                data = base64.b64decode(encoded, validate=True)
                if len(data) > length:
                    raise BoxError("invalid_response")
                if self._need_bindings().policy.scan_inbound(data) is False:
                    raise BoxError("artifact_scan_blocked")
                return data
            except BoxError as error:
                if error.code != "truncated" or length == 1:
                    raise
                length //= 2
            except (ValueError, TypeError):
                raise BoxError("invalid_response") from None
        raise BoxError("truncated")

    def get_file(self, path, offset=0, length=12288):
        # Shared runner_client owns binary file reads and the exact dd invocation.
        if not isinstance(offset, int) or offset < 0 or not isinstance(length, int) or not 0 < length <= 12288:
            raise ToolDenied("arg_rule:runner")
        data = self._runner().get_file(self, self.env, path, offset, length)
        if not isinstance(data, bytes) or len(data) > length:
            raise BoxError("invalid_response")
        if self._need_bindings().policy.scan_inbound(data) is False:
            raise BoxError("artifact_scan_blocked")
        return data

    def cancel(self, env: dict, job_id: str):
        return self.sh(env, "cancel", job_id)

    def lookup(self, env: dict, idempotency_key: str):
        return self.sh(env, "lookup", idempotency_key)

    def hibernate(self, env: dict | None = None, *, idle_s=0, queue_len=0, active_tokens=0, active_jobs=0):
        self._same_env(env)
        threshold = self.limit("sbx_idle_suspend_min", 0)
        if threshold <= 0 or idle_s < threshold * 60 or queue_len or active_tokens or active_jobs:
            return False
        busy = self.sh(env, "busy")
        if not isinstance(busy, dict) or busy.get("busy") is not False:
            return False
        with self._scope("hibernate"):
            self._last_listing = tool_payload(self.call_tool("box_list", {}))
            self._need_bindings().guard.identify(self.env, self._last_listing)
            self.call_tool("box_hibernate", self._machine_args("box_hibernate"))
        return True

    suspend = hibernate


BoxDriver = BoxMCPDriver

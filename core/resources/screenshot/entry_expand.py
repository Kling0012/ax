"""S-25.26: provider-neutral screenshot planning and artifact collection.

The environment selector owns leases, environment ordering and env.selection.
This module returns candidate fields; it never chooses a provider or wakes one.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import ipaddress
import json
import math
import re
import socket
import struct
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit

from axcommon import schemas

SCHEMA_ROOT = "ext/b18b/screenshot/"
RUN = "run:entries/screenshot"
EXPAND = "expand:config/sandbox-entry-steps.yaml#screenshot"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FETCH_BYTES = 12288
DEFAULTS = {"full_page": True, "width": 1280, "height": 800, "wait_s": 3, "headed": False}
DEFAULT_TABLE = Path(__file__).resolve().parents[3] / "config/sandbox-entry-steps.yaml"


class ScreenshotError(ValueError):
    def __init__(self, reason: str, state: str = "rejected"):
        # Never include request data or provider exception text.
        super().__init__(reason)
        self.reason = reason
        self.state = state


class ToolDriver(Protocol):
    def call_tool(self, name: str, args: dict, timeout_s: float) -> dict: ...


class RunnerPort(Protocol):
    """Internal bridge to the sandbox runner, not a new wire API.

    run_entry waits/polls using the normal runner client and returns descriptors
    {name: {size: int, sha256: str}} for out/page.png and out/meta.json.
    fetch returns decoded bytes from the runner's bounded fetch operation.
    Each bridge must enforce the supplied timeout, without trying another impl.
    """
    def run_entry(self, args: dict, timeout_s: float) -> Mapping[str, dict]: ...
    def fetch(self, name: str, offset: int, size: int, timeout_s: float) -> bytes: ...


@dataclass(frozen=True)
class Choice:
    impl: str | None
    reason: str | None = None
    detail: str | None = None

    def candidate_fields(self) -> dict:
        if self.impl:
            return {"impl": self.impl}
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class Plan:
    """A JSON snapshot: later probe/request mutations cannot change a job."""
    impl: str
    source: str
    args_json: str
    steps_json: str
    call_timeout_s: float

    @property
    def args(self) -> dict:
        return json.loads(self.args_json)

    @property
    def steps(self) -> list[dict]:
        return json.loads(self.steps_json)


def load_table(path: Path = DEFAULT_TABLE) -> dict:
    import yaml

    try:
        table = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        schemas.validate(SCHEMA_ROOT + "entry-steps", table)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ScreenshotError("entry_table_invalid") from None
    return table


def validate_url(url: str) -> None:
    # Inspect lexical addresses only. No DNS request or outbound socket is used.
    if re.search(r"[\x00-\x20\x7f\\]", url):
        raise ScreenshotError("url_denied")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        if parsed.scheme not in {"http", "https"} or not host or parsed.port not in {None, 80, 443}:
            raise ValueError
        if "@" in parsed.netloc or "%" in host:
            raise ValueError
        if host == "localhost" or host.endswith((".localhost", ".ts.net", ".internal", ".local")):
            raise ValueError
        address = None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                # Includes integer, octal and hexadecimal IPv4 spellings.
                address = ipaddress.ip_address(socket.inet_aton(host))
            except OSError:
                if "." not in host or ":" in host or not re.fullmatch(r"[a-z0-9.-]+", host):
                    raise ValueError
        if address is not None:
            if not address.is_global or (isinstance(address, ipaddress.IPv6Address)
                                         and address.ipv4_mapped and not address.ipv4_mapped.is_global):
                raise ValueError
        blocked = ("token", "key", "secret", "pass" + "word", "passwd", "auth", "session", "sig", "signature", "access")
        if any(any(part in unquote(key).lower() for part in blocked)
               for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ScreenshotError("url_denied") from None


def _checked(callback: Callable, *args: Any, reason: str = "secret_detected") -> None:
    if callback(*args) is False:
        raise ScreenshotError(reason)


def validate_request(request: dict, scan_outbound: Callable[[Any], None]) -> dict:
    if request.get("sensitivity") != "public" or request.get("secrets"):
        raise ScreenshotError("sensitivity_denied")
    entry = request.get("entry", {})
    if not isinstance(entry, dict) or entry.get("kind") != "entrypoint" or entry.get("name") != "screenshot":
        raise ScreenshotError("entry_unsupported")
    args = entry.get("args", {})
    try:
        schemas.validate(SCHEMA_ROOT + "args", args)
    except ValueError:
        raise ScreenshotError("invalid_args") from None
    args = {**DEFAULTS, **args}
    if not math.isfinite(args["wait_s"]):
        raise ScreenshotError("invalid_args")
    validate_url(args["url"])
    # Mandatory shared S-15.3 / sandbox_policy callback; never a permissive default.
    _checked(scan_outbound, copy.deepcopy(request))
    return args


def select_implementation(env: Mapping, request: Mapping, probe: Mapping,
                          implemented_tools: set[str] | frozenset[str]) -> Choice:
    """Call at the selector's entry-shape stage with its latest probe snapshot."""
    entry = request.get("entry", {})
    if entry.get("kind") != "entrypoint" or entry.get("name") != "screenshot":
        return Choice(None, "entry_unsupported", "entry_not_registered")
    implementations = env.get("entrypoints", {}).get("screenshot", [])
    checked = probe.get("capabilities_checked", {})
    full_required = "web-screenshot-full" in request.get("requires", [])
    for implementation in implementations:
        if implementation == RUN and all(checked.get(cap) == "ok" for cap in ("ax-runner", "web-screenshot-full")):
            return Choice("run")
        if (implementation == EXPAND and not full_required
                and checked.get("web-screenshot") == "ok"
                and {"browser_navigate", "screenshot"} <= set(implemented_tools)):
            return Choice("expand")
    return Choice(None, "entry_unsupported", "no_usable_impl:ax-runner,web-screenshot-full,impl")


def prepare_entry(env: dict, request: dict, probe: dict, implemented_tools: set[str], *,
                  scan_outbound: Callable[[Any], None], check_tool: Callable[[str, dict], None],
                  table: dict | None = None, sandbox_steps_max: int = 50) -> Plan:
    args = validate_request(request, scan_outbound)
    choice = select_implementation(env, request, probe, implemented_tools)
    if choice.impl is None:
        raise ScreenshotError("entry_unsupported")
    steps: list[dict] = []
    if choice.impl == "expand":
        if not env.get("free_command", False):
            raise ScreenshotError("entry_unsupported")
        table = copy.deepcopy(table) if table is not None else load_table()
        try:
            schemas.validate(SCHEMA_ROOT + "entry-steps", table)
        except ValueError:
            raise ScreenshotError("entry_table_invalid") from None
        steps = copy.deepcopy(table["screenshot"]["steps"])
        steps[0]["args"]["url"] = args["url"]
        steps[1]["seconds"] = args["wait_s"]
        validate_steps(steps, check_tool=check_tool, scan_outbound=scan_outbound,
                       sandbox_steps_max=sandbox_steps_max)
    try:
        call_timeout = float(env["limits"]["call_timeout_s"])
        if not math.isfinite(call_timeout) or call_timeout <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ScreenshotError("invalid_call_timeout") from None
    return Plan(choice.impl, env["id"], json.dumps(args), json.dumps(steps), call_timeout)


def validate_steps(steps: list[dict], *, check_tool: Callable[[str, dict], None],
                   scan_outbound: Callable[[Any], None], sandbox_steps_max: int = 50) -> None:
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise ScreenshotError("invalid_steps")
    if sum("tool" in step for step in steps) > sandbox_steps_max:
        raise ScreenshotError("steps_limit")
    for step in steps:
        if "tool" in step:
            if set(step) != {"tool", "args"} or not isinstance(step["args"], dict):
                raise ScreenshotError("invalid_steps")
            _checked(check_tool, step["tool"], copy.deepcopy(step["args"]), reason="tool_denied")
            _checked(scan_outbound, copy.deepcopy(step["args"]))
        elif set(step) != {"adapter", "seconds"} or step["adapter"] != "wait":
            raise ScreenshotError("invalid_steps")


def inspect_png(data: bytes, max_bytes: int = 8388608) -> tuple[int, int]:
    if not isinstance(data, bytes) or not 33 <= len(data) <= max_bytes or data[:8] != PNG_SIGNATURE:
        raise ScreenshotError("bad_png", "failed")
    size, kind = struct.unpack(">I4s", data[8:16])
    if size != 13 or kind != b"IHDR" or zlib.crc32(data[12:29]) != struct.unpack(">I", data[29:33])[0]:
        raise ScreenshotError("bad_png", "failed")
    width, height = struct.unpack(">II", data[16:24])
    if not width or not height:
        raise ScreenshotError("bad_png", "failed")
    return width, height


def image_from_response(response: dict, max_bytes: int = 8388608) -> bytes:
    if not isinstance(response, dict) or not isinstance(response.get("content"), list):
        raise ScreenshotError("no_image", "failed")
    for content in response["content"]:
        if not isinstance(content, dict):
            continue
        if content.get("type") == "image" and content.get("mimeType") == "image/png":
            encoded = content.get("data")
            if not isinstance(encoded, str) or len(encoded) > 4 * ((max_bytes + 2) // 3):
                raise ScreenshotError("bad_png", "failed")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise ScreenshotError("bad_png", "failed") from None
            inspect_png(data, max_bytes)
            return data
    raise ScreenshotError("no_image", "failed")


class ScreenshotExecutor:
    """Execute an already selected plan; no probing, provider choice or fallback.

    close_owned_tab receives the navigation response and must close only the tab
    identified by that response, using the driver's ordinary policy/audit path.
    This binding remains mandatory until the provider schema is measured.
    """
    def __init__(self, *, check_tool: Callable[[str, dict], None],
                 scan_outbound: Callable[[Any], None], scan_artifact: Callable[[bytes], None],
                 close_owned_tab: Callable[[dict, float], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[float], None] = time.sleep,
                 utcnow: Callable[[], str] | None = None,
                 timeout_s: float = 60, max_bytes: int = 8388608, max_height_px: int = 16384):
        if not math.isfinite(timeout_s) or timeout_s <= 0 or max_bytes < 33 or max_height_px <= 0:
            raise ValueError("invalid_screenshot_limits")
        self.check_tool = check_tool
        self.scan_outbound = scan_outbound
        self.scan_artifact = scan_artifact
        self.close_owned_tab = close_owned_tab
        self.clock, self.wait = clock, wait
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        self.timeout_s, self.max_bytes = timeout_s, max_bytes
        self.max_height_px = max_height_px

    def _remaining(self, deadline: float, call_timeout_s: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ScreenshotError("timed_out", "timed_out")
        return min(remaining, call_timeout_s)

    def execute(self, plan: Plan, *, driver: ToolDriver | None = None,
                runner: RunnerPort | None = None) -> tuple[bytes, dict]:
        deadline = self.clock() + self.timeout_s
        try:
            if plan.impl == "run":
                if runner is None:
                    raise NotImplementedError("sandbox_runner_binding")
                descriptors = runner.run_entry(plan.args, self._remaining(deadline, plan.call_timeout_s))
                png = self._fetch(runner, "out/page.png", descriptors, deadline, plan.call_timeout_s)
                raw_meta = self._fetch(runner, "out/meta.json", descriptors, deadline, plan.call_timeout_s)
                self._remaining(deadline, plan.call_timeout_s)
                inspect_png(png, self.max_bytes)
                try:
                    meta = json.loads(raw_meta)
                except (ValueError, UnicodeError):
                    raise ScreenshotError("bad_metadata", "failed") from None
                self._validate_run_meta(meta, plan.args, png)
                meta = {**meta, "source": plan.source, "impl": "run"}
            elif plan.impl == "expand":
                if driver is None:
                    raise NotImplementedError("sandbox_tool_binding")
                if self.close_owned_tab is None:
                    raise NotImplementedError("FACTS F-21.2: owned-tab cleanup inputSchema not measured")
                png = self._expand(plan, driver, deadline)
                width, height = inspect_png(png, self.max_bytes)
                meta = {"final_url": plan.args["url"], "title": None, "http_status": None,
                        "width": width, "height": height, "captured_at": self.utcnow(),
                        "mode": "viewport", "source": plan.source, "impl": "expand",
                        "ignored_args": ["full_page", "width", "height", "headed"]}
            else:
                raise ScreenshotError("entry_unsupported")
            try:
                schemas.validate(SCHEMA_ROOT + "meta", meta)
            except ValueError:
                raise ScreenshotError("bad_metadata", "failed") from None
            _checked(self.scan_artifact, png)
            _checked(self.scan_artifact, json.dumps(meta, ensure_ascii=False).encode("utf-8"))
            return png, meta
        except TimeoutError:
            raise ScreenshotError("timed_out", "timed_out") from None
        except Exception as error:
            if getattr(error, "code", None) == "timed_out":
                raise ScreenshotError("timed_out", "timed_out") from None
            raise

    def _expand(self, plan: Plan, driver: ToolDriver, deadline: float) -> bytes:
        navigation = None
        failure = None
        try:
            response = {}
            for step in plan.steps:
                if "adapter" in step:
                    remaining = self._remaining(deadline, plan.call_timeout_s)
                    duration = step["seconds"]
                    if duration >= deadline - self.clock():
                        raise ScreenshotError("timed_out", "timed_out")
                    self.wait(duration)
                    self._remaining(deadline, plan.call_timeout_s)
                    continue
                name, args = step["tool"], step["args"]
                _checked(self.check_tool, name, copy.deepcopy(args), reason="tool_denied")
                _checked(self.scan_outbound, copy.deepcopy(args))
                response = driver.call_tool(name, args, self._remaining(deadline, plan.call_timeout_s))
                if name == "browser_navigate":
                    navigation = response
                self._remaining(deadline, plan.call_timeout_s)
                if response.get("isError"):
                    raise ScreenshotError("tool_error", "failed")
            return image_from_response(response, self.max_bytes)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if navigation is not None:
                try:
                    # Even after expiry, attempt bounded cleanup of our own tab.
                    cleanup_s = min(plan.call_timeout_s, self.timeout_s,
                                    max(deadline - self.clock(), 0.001))
                    self.close_owned_tab(navigation, cleanup_s)
                except Exception:
                    if failure is None:
                        raise ScreenshotError("tab_cleanup_failed", "failed") from None

    def _fetch(self, runner: RunnerPort, name: str, descriptors: Mapping[str, dict],
               deadline: float, call_timeout_s: float) -> bytes:
        descriptor = descriptors.get(name, {})
        mismatch = "bad_png" if name == "out/page.png" else "artifact_mismatch"
        size, digest = descriptor.get("size"), descriptor.get("sha256")
        if type(size) is not int or not 0 < size <= self.max_bytes or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ScreenshotError(mismatch, "failed")
        parts = []
        for offset in range(0, size, FETCH_BYTES):
            amount = min(FETCH_BYTES, size - offset)
            chunk = runner.fetch(name, offset, amount, self._remaining(deadline, call_timeout_s))
            self._remaining(deadline, call_timeout_s)
            if not isinstance(chunk, bytes) or len(chunk) != amount:
                raise ScreenshotError(mismatch, "failed")
            parts.append(chunk)
        data = b"".join(parts)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ScreenshotError(mismatch, "failed")
        return data

    def _validate_run_meta(self, meta: Any, args: dict, png: bytes) -> None:
        # date-time format checking is optional in minimal jsonschema installs.
        try:
            captured = meta["captured_at"]
            if not isinstance(captured, str) or "T" not in captured:
                raise ValueError
            timestamp = datetime.fromisoformat(captured.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError
        except (TypeError, KeyError, ValueError):
            raise ScreenshotError("bad_metadata", "failed") from None
        width, height = inspect_png(png, self.max_bytes)
        expected_mode = "fullpage" if args["full_page"] else "viewport"
        if (not isinstance(meta, dict) or meta.get("mode") != expected_mode
                or meta.get("width") != width or meta.get("height") != height
                or height > self.max_height_px or not isinstance(meta.get("final_url"), str)
                or not isinstance(meta.get("title"), str) or type(meta.get("http_status")) is not int
                or not isinstance(meta.get("captured_at"), str) or meta.get("ignored_args") != []):
            raise ScreenshotError("bad_metadata", "failed")


def save_artifacts(root: Path, job_id: str, png: bytes, meta: dict, *,
                   scan_artifact: Callable[[bytes], None], max_bytes: int = 8388608) -> list[dict]:
    """Create job-scoped outputs exclusively, after both payloads pass scanning."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", job_id):
        raise ScreenshotError("invalid_job_id")
    inspect_png(png, max_bytes)
    payloads = {"page.png": png, "meta.json": json.dumps(meta, ensure_ascii=False).encode("utf-8")}
    for payload in payloads.values():
        if len(payload) > max_bytes:
            raise ScreenshotError("artifact_mismatch", "failed")
        _checked(scan_artifact, payload)
    directory = Path(root) / job_id
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ScreenshotError("artifact_exists", "failed") from None
    manifest = []
    for name, payload in payloads.items():
        with (directory / name).open("xb") as stream:
            stream.write(payload)
        manifest.append({"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    return manifest

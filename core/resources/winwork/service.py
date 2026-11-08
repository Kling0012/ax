"""S-25.9 Windows tools. No transport, environment selection, or worker fallback."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml

from axcommon import schemas
from axcommon.policy import SENSITIVITIES, allowed

SCHEMA = "ext/b18b/winwork/windows-tools"
REQUEST_SCHEMA = "ext/b18b/winwork/call"


def load_tools(path: str | Path | None = None) -> list[dict]:
    """Read the package catalogue, not config/tools.yaml or environments.yaml."""
    if path is None:
        path = Path(os.environ.get("AX_CONFIG_DIR", "/ax/config")) / "windows-tools.yaml"
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    schemas.validate(SCHEMA, document)
    tools = document["tools"]
    if len({tool["id"] for tool in tools}) != len(tools):
        raise ValueError("duplicate tool id")
    # One registered id per desktop; aliases would bypass max_concurrent=1.
    if len({tool["location"] for tool in tools}) != len(tools):
        raise ValueError("duplicate desktop location")
    return deepcopy(tools)


def register_tools_config(*_args, **_kwargs):
    """The config/tools.yaml include contract is not specified (S-25.9)."""
    raise NotImplementedError("config/tools.yaml integration requires an agreed include contract")


@dataclass(frozen=True)
class DesktopState:
    """Trusted executor observation, obtained again while holding the desktop slot.

    model is resolved by the executor, never supplied by the caller. It contains
    the actual LLM family's data_classes and tier. Unknown family means public.
    idle must be measured; an unavailable or unknown desktop is never started.
    """
    available: bool = False
    idle: bool | None = None
    owner_use: bool | None = None
    model: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionResult:
    """run returns only after terminal completion; lost requires reconciliation."""
    status: str
    output: Mapping[str, Any] = field(default_factory=dict)


class Executor(Protocol):
    def probe(self, tool: Mapping, location: str) -> DesktopState: ...
    def run(self, tool: Mapping, location: str, request: Mapping) -> ExecutionResult: ...


class EventWriter(Protocol):
    def emit(self, kind: str, subject: str, data: dict, **kwargs) -> Any: ...


class UnconfiguredExecutor:
    """No SSH, scheduled task, GUI, or remote service is guessed or started."""
    def probe(self, tool, location):
        raise NotImplementedError("Windows transport and owner-use probe are not specified")

    def run(self, tool, location, request):
        raise NotImplementedError("Windows execution transport is not specified")


class WindowsTools:
    """Internal call boundary for the b18b-4 control API extension.

    Use a shared state_dir on the resources volume for every instance. flock is
    nonblocking and spans threads/processes. A durable uncertain marker prevents
    retry after an ambiguous execution or controller crash. Only reconcile() can
    clear it after the executor confirms idle. No calls are queued or retried.
    """
    def __init__(self, tools: list[dict], *, profile: Mapping[str, str],
                 state_dir: str | Path, events: EventWriter,
                 executor: Executor | None = None):
        schemas.validate(SCHEMA, {"tools": tools})
        if len({t["id"] for t in tools}) != len(tools):
            raise ValueError("duplicate tool id")
        if len({t["location"] for t in tools}) != len(tools):
            raise ValueError("duplicate desktop location")
        self._tools = {t["id"]: deepcopy(t) for t in tools}
        self._profile = dict(profile)
        resolved = [self._profile.get(t["location"]) for t in tools]
        resolved = [value for value in resolved if isinstance(value, str) and value.strip()]
        if len(set(resolved)) != len(resolved):
            raise ValueError("duplicate resolved desktop location")
        self._directory = Path(state_dir)
        self._events = events
        self._executor = executor or UnconfiguredExecutor()
        self.event_failures = 0

    @contextmanager
    def _slot(self, tool_id: str):
        self._directory.mkdir(parents=True, exist_ok=True)
        with (self._directory / f"{tool_id}.lock").open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            try:
                handle.seek(0)
                pending = handle.read().strip() not in ("", "idle")
                yield (handle, pending)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _mark(handle, value: str):
        handle.seek(0)
        handle.truncate()
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())

    def _event(self, tool_id: str, status: str, reason: str, sensitivity: str | None):
        # Never log instructions, target URLs/apps, profile values, output or errors.
        try:
            self._events.emit("tool.windows_call", f"tool:{tool_id or 'unspecified'}", {
                "tool_id": tool_id or None, "status": status,
                "reason": reason, "sensitivity": sensitivity,
            })
        except Exception:
            self.event_failures += 1
            return False
        return True

    def _result(self, tool_id, status, reason, sensitivity, output=None):
        self._event(tool_id, status, reason, sensitivity)
        result = {"tool_id": tool_id or None, "status": status, "reason": reason}
        if output is not None:
            result["output"] = dict(output)
        return result

    @staticmethod
    def _data_allowed(tool, state, sensitivity):
        if sensitivity not in tool["data_classes"]:
            return False
        model = state.model if isinstance(state.model, Mapping) else None
        family_classes = model.get("data_classes") if model else None
        if not isinstance(family_classes, (list, tuple)) or not family_classes:
            family_classes = ["public"]
        # Reject malformed trusted metadata conservatively, rather than widen it.
        if any(c not in SENSITIVITIES for c in family_classes):
            return False
        if sensitivity not in family_classes:
            return False
        if sensitivity == "public":
            return True
        if model is None:
            return False
        tier = model.get("tier")
        if sensitivity == "sensitive" and not isinstance(tier, str):
            return False
        return allowed(sensitivity, {"kind": "model", "tier": tier})

    def call(self, request: dict) -> dict:
        """Require an explicit id and URL/app target even for user_requested calls."""
        try:
            schemas.validate(REQUEST_SCHEMA, request)
        except schemas.ValidationFailed:
            return self._result("", "rejected", "invalid_request", None)
        request = deepcopy(request)
        tool_id, sensitivity = request["tool_id"], request["sensitivity"]
        tool = self._tools.get(tool_id)
        if tool is None:
            # Unknown caller text must not enter the timeline subject.
            return self._result("", "rejected", "unknown_tool", sensitivity)
        location = self._profile.get(tool["location"])
        if not isinstance(location, str) or not location.strip():
            return self._result(tool_id, "unavailable", "profile_missing", sensitivity)
        try:
            with self._slot(tool_id) as slot:
                if slot is None:
                    return self._result(tool_id, "unavailable", "slot_busy", sensitivity)
                handle, pending = slot
                if pending:
                    return self._result(tool_id, "unavailable", "reconciliation_required", sensitivity)
                try:
                    state = self._executor.probe(deepcopy(tool), location)
                except NotImplementedError:
                    return self._result(tool_id, "unavailable", "not_implemented", sensitivity)
                except Exception:
                    return self._result(tool_id, "unavailable", "probe_failed", sensitivity)
                if not isinstance(state, DesktopState) or state.available is not True:
                    return self._result(tool_id, "unavailable", "desktop_unavailable", sensitivity)
                if state.owner_use is not False:
                    reason = "owner_use" if state.owner_use is True else "owner_use_unknown"
                    return self._result(tool_id, "unavailable", reason, sensitivity)
                if state.idle is not True:
                    return self._result(tool_id, "unavailable", "desktop_busy", sensitivity)
                if not self._data_allowed(tool, state, sensitivity):
                    return self._result(tool_id, "rejected", "data_class_denied", sensitivity)
                # Persist intent before the external action. A crash never implies idle.
                self._mark(handle, "uncertain")
                if not self._event(tool_id, "running", "accepted", sensitivity):
                    self._mark(handle, "idle")
                    return self._result(tool_id, "unavailable", "timeline_unavailable", sensitivity)
                try:
                    result = self._executor.run(deepcopy(tool), location, request)
                except NotImplementedError:
                    # The transport contract requires this before accepting any action.
                    self._mark(handle, "idle")
                    return self._result(tool_id, "unavailable", "not_implemented", sensitivity)
                except Exception:
                    return self._result(tool_id, "lost", "execution_unknown", sensitivity)
                if (not isinstance(result, ExecutionResult)
                        or result.status not in ("succeeded", "failed")
                        or not isinstance(result.output, Mapping)):
                    return self._result(tool_id, "lost", "execution_unknown", sensitivity)
                self._mark(handle, "idle")
                return self._result(tool_id, result.status, "completed", sensitivity, result.output)
        except OSError:
            return self._result(tool_id, "unavailable", "slot_unavailable", sensitivity)

    def reconcile(self, tool_id: str) -> bool:
        """Trusted integration only: clear uncertain state after a fresh idle probe."""
        tool = self._tools.get(tool_id)
        if tool is None:
            return False
        location = self._profile.get(tool["location"])
        if not isinstance(location, str) or not location.strip():
            return False
        try:
            with self._slot(tool_id) as slot:
                if slot is None:
                    return False
                handle, pending = slot
                if not pending:
                    return True
                state = self._executor.probe(deepcopy(tool), location)
                if not isinstance(state, DesktopState) or state.available is not True or state.idle is not True:
                    return False
                self._mark(handle, "idle")
                self._event(tool_id, "reconciled", "idle_confirmed", None)
                return True
        except Exception:
            return False

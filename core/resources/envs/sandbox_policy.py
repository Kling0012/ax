"""Shared, fail-closed checks for sandbox calls (S-25.17 and S-25.19)."""
from __future__ import annotations

import json
import os
import pwd
import re
import stat
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from axcommon import schemas

from core.resources.box.protocol import BoxError, ToolDenied

KEY_SLOTS = {
    (provider, purpose): f"{provider}-key-{purpose}"
    for provider in ("box", "instabox") for purpose in ("ax", "worker")
}
KEY_PURPOSE_BY_CALLER = {"adapter": "ax", "driver": "ax", "gate": "worker", "backend": "worker"}
NEVER_ALLOW_ACTIONS = frozenset({"type_from_file"})
ACTIONS = frozenset({"click", "double_click", "right_click", "triple_click", "type", "key", "hotkey", "scroll", "drag", "launch_app", "wait", "locate"})
FORBIDDEN_PATTERNS = ("box connect", "@simular-ai/box", "api.instabox.sh", "mcp.box.simular.ai")


DENIED_TOOLS = frozenset({"machine_create", "machine_delete", "machine_upgrade", "billing_write",
                          "box_create", "box_ensure", "box_delete", "box_upgrade", "box_save",
                          "box_snapshots", "box_restore_paths"})
_SLOT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class SandboxPolicy:
    """Bind a caller to one key purpose and an existing configured redactor."""

    def __init__(self, *, caller: str, secrets_dir: str | os.PathLike, redactor: Any,
                 tools_path: str | os.PathLike | None = None):
        if caller not in KEY_PURPOSE_BY_CALLER or not callable(getattr(redactor, "scan", None)):
            raise ValueError("invalid sandbox policy configuration")
        self._purpose = KEY_PURPOSE_BY_CALLER[caller]
        self._slots = Path(secrets_dir) / "hq-slots"
        self._redactor = redactor
        import yaml
        path = Path(tools_path) if tools_path else Path(__file__).resolve().parents[3] / "config" / "sandbox-tools.yaml"
        try:
            table = yaml.safe_load(path.read_text(encoding="utf-8"))
            schemas.validate("sandbox-tools", table)
        except Exception:
            raise ValueError("invalid sandbox tool table") from None
        tools = table["tools"]
        for name, row in tools.items():
            impl = row["instabox"]
            if (self._hard_denied(name) or self._hard_denied(row["box"])
                    or (impl is not None and self._denied_http(impl))):
                raise ValueError("sandbox tool table contains a denied operation")
        if any(pattern not in table["forbidden_patterns"] for pattern in FORBIDDEN_PATTERNS):
            raise ValueError("sandbox tool table omits a forbidden pattern")
        self._tools = tools
        self._patterns = tuple(table["forbidden_patterns"])
        rules = tools.get("computer_act", {}).get("arg_rules", {})
        self._computer_enabled = (bool(rules) and not (set(rules["action_allow"]) & NEVER_ALLOW_ACTIONS)
                                  and {"field", "path"} <= set(rules["deny_keys"]))
        self._computer_rules = rules

    @staticmethod
    def _hard_denied(name: str) -> bool:
        return name in DENIED_TOOLS or name.startswith(("snapshot_", "publish_"))

    @staticmethod
    def _denied_http(impl: str) -> bool:
        method, sep, path = impl.partition(" ")
        if not sep or method not in {"GET", "POST", "PUT"} or not path.startswith("/"):
            return True
        if any(part in path.lower() for part in ("/publish", "/snapshot", "/images")):
            return True
        return method != "GET" and (path == "/machines" or path.startswith(("/orgs", "/billing")))

    def tool_names(self, *, path: str) -> tuple[str, ...]:
        """Return table names permitted on a route before capability and token filtering."""
        return tuple(name for name in self._tools
                     if path in self._tools[name]["paths"]
                     and (name != "computer_act" or self._computer_enabled))

    def tool_spec(self, name: str, *, path: str) -> Mapping[str, Any]:
        """Return a copy of provider mappings and required capability for a route."""
        self.check_tool(name, path=path)
        import copy
        return copy.deepcopy(self._tools[name])

    def check_tool(self, name: str, *, path: str) -> None:
        row = self._tools.get(name) if isinstance(name, str) else None
        if row is None or path not in row["paths"] or (name == "computer_act" and not self._computer_enabled):
            raise ToolDenied("not_in_table")

    def check_job(self, environment: Mapping[str, Any], spec: Mapping[str, Any], *,
                  required_credentials: tuple[str, ...] = (), repo_is_public: Callable[[str], bool]) -> None:
        """Validate a job before transport; repository lookup must confirm public visibility."""
        if not isinstance(environment, Mapping) or not isinstance(spec, Mapping):
            raise BoxError("credential_invalid")
        input_classes = spec.get("input_sensitivities", ())
        if (not isinstance(input_classes, (list, tuple)) or spec.get("sensitivity") != "public"
                or any(x != "public" for x in input_classes)
                or spec.get("output_sensitivity", "public") not in (None, "public")):
            raise BoxError("sensitivity_denied")
        credentials = environment.get("work_credentials")
        refs = spec.get("secrets", [])
        if not isinstance(credentials, list) or not isinstance(refs, (list, tuple)) or not isinstance(required_credentials, (list, tuple)):
            raise BoxError("credential_invalid")
        slots = []
        for item in credentials:
            if (not isinstance(item, Mapping) or not schemas.is_valid("environment", dict(item), "/$defs/credential")
                    or not isinstance(item.get("slot"), str) or not _SLOT.fullmatch(item["slot"])):
                raise BoxError("credential_invalid")
            slots.append(item["slot"])
        if any(not isinstance(ref, str) or not _SLOT.fullmatch(ref) for ref in [*refs, *required_credentials]):
            raise BoxError("credential_invalid")
        if not set(refs) <= set(slots) or not set(required_credentials) <= set(refs):
            raise BoxError("credential_missing")
        inputs = spec.get("inputs", [])
        if not isinstance(inputs, list):
            raise BoxError("repo_not_public")
        for item in inputs:
            if isinstance(item, Mapping) and item.get("sensitivity", "public") != "public":
                raise BoxError("sensitivity_denied")
            ref = item.get("ref") if isinstance(item, Mapping) else item
            if not isinstance(ref, str):
                raise BoxError("repo_not_public")
            if ref.startswith("git:"):
                repo, sep, revision = ref[4:].rpartition("@")
                try:
                    public = bool(repo and sep and revision and repo_is_public(repo) is True)
                except Exception:
                    public = False
                if not public:
                    raise BoxError("repo_not_public")
        def has_place(value: Any) -> bool:
            if isinstance(value, Mapping):
                return "place" in value or any(has_place(v) for v in value.values())
            if isinstance(value, (list, tuple)):
                return any(has_place(v) for v in value)
            return False
        if has_place(spec):
            raise BoxError("credential_invalid")
        try:
            text = json.dumps(spec, ensure_ascii=False)
        except (TypeError, ValueError):
            raise BoxError("scan_blocked") from None
        if any(isinstance(c.get("place"), str) and c["place"] and c["place"] in text for c in credentials):
            raise BoxError("credential_invalid")
        if not self.scan_outbound(spec):
            raise BoxError("scan_blocked")

    def authorize_call(self, name: str, args: Any, *, path: str, root: str, job_id: str | None = None) -> None:
        """Apply name, argument, path and outbound checks before a tool transport."""
        from pathlib import PurePosixPath
        self.check_tool(name, path=path)
        self.check_tool_args(name, args)
        if name in {"write_file", "edit_file"}:
            base = PurePosixPath(root)
            target = args.get("path")
            if not base.is_absolute() or ".." in base.parts or not isinstance(target, str):
                raise ToolDenied("path_condition")
            if path == "G":
                if not isinstance(job_id, str) or not _SLOT.fullmatch(job_id):
                    raise ToolDenied("path_condition")
                base = base / "jobs" / job_id
            candidate = PurePosixPath(target)
            if not candidate.is_absolute() or ".." in candidate.parts or not candidate.is_relative_to(base) or candidate == base:
                raise ToolDenied("path_condition")
        if name == "browser_navigate":
            try:
                url = urlsplit(args.get("url", ""))
                if url.username is not None or url.password is not None:
                    raise BoxError("scan_blocked")
            except (TypeError, ValueError):
                raise BoxError("scan_blocked") from None
        if not self.scan_outbound(args):
            raise BoxError("scan_blocked")

    def read_key(self, provider: str, purpose: str) -> str:
        if purpose != self._purpose or (provider, purpose) not in KEY_SLOTS:
            raise BoxError("credential_purpose_mismatch")
        try:
            fd = os.open(self._slots / KEY_SLOTS[provider, purpose], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise BoxError("credential_missing") from None
        except OSError:
            raise BoxError("credential_insecure") from None
        try:
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            try:
                hq_uid = pwd.getpwnam("ax-hq").pw_uid
            except KeyError:
                hq_uid = -1
            if not stat.S_ISREG(info.st_mode) or not (mode == 0o600 or (mode == 0o640 and info.st_uid == hq_uid)):
                raise BoxError("credential_insecure")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        if len(raw) > 4096:
            raise BoxError("credential_invalid")
        try:
            key = raw.decode("ascii").replace("\r", "").replace("\n", "")
        except UnicodeDecodeError:
            raise BoxError("credential_invalid") from None
        valid = (re.fullmatch(r"[!-~]{16,512}", key) if provider == "box" else
                 re.fullmatch(r"ibk_[A-Za-z0-9_-]{12,252}", key))
        if not valid:
            raise BoxError("credential_invalid")
        return key

    def check_tool_args(self, name: str, args: Any) -> None:
        def deny():
            raise ToolDenied("arg_rule:" + name)
        if not isinstance(args, dict):
            deny()
        if name != "computer_act":
            return
        if not self._computer_enabled:
            deny()
        rules = self._computer_rules
        if "action" not in args and "actions" not in args:
            deny()
        targets = [args]
        if "actions" in args:
            actions = args["actions"]
            if not isinstance(actions, list) or len(actions) > rules["actions_max"]:
                deny()
            targets.extend(actions)
        for target in targets:
            if not isinstance(target, dict) or set(rules["deny_keys"]) & target.keys():
                deny()
            if "action" in target and (not isinstance(target["action"], str) or target["action"] not in rules["action_allow"]):
                deny()

    def scan_outbound(self, value: Any) -> bool:
        try:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return False
        if any(pattern in text for pattern in self._patterns):
            raise BoxError("forbidden_command")
        return self._scan(text)

    def scan_inbound(self, value: bytes) -> bool:
        if not isinstance(value, bytes):
            return False
        return self._scan(value.decode("utf-8", "replace"))

    def _scan(self, text: str) -> bool:
        try:
            if (not self._redactor.index_path or not self._redactor.pii_list_path
                    or not self._redactor.index_ok or not self._redactor.pii_list_ok):
                return False
            if not self._redactor.reload_index_if_changed() or not self._redactor.reload_pii_list():
                return False
            return not self._redactor.scan(text)
        except Exception:
            return False

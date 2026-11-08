"""Pre-execution gates for S-19.5. No network or process execution."""
from __future__ import annotations

import fnmatch
import ipaddress
from pathlib import PurePosixPath


class GateError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def safe_path(path):
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise GateError("changed_files_invalid")
    parts = path.split("/")
    if path.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise GateError("changed_files_invalid")
    return path


def matches(path, pattern):
    return path == pattern.rstrip("/") or path.startswith(pattern.rstrip("/") + "/") or fnmatch.fnmatchcase(path, pattern)


def inspect_changes(request):
    if request.get("files_complete") is not True or not isinstance(request.get("changed_files"), list):
        raise GateError("changed_files_unavailable")
    paths = []
    forbidden = [".github/", "scripts/", "build.sh", "*.bat", "Dockerfile*", "**/Dockerfile*",
                 "docker-compose*.yml", "docker-compose*.yaml", "compose*.yaml", "compose*.yml",
                 "*.spec", "pnpm-lock.yaml", "uv.lock", "package.json", "pyproject.toml", "*.csproj", ".env*", "**/.env*"]
    extra = request.get("untrusted_forbidden_paths", [])
    if not isinstance(extra, list) or any(not isinstance(p, str) or not p for p in extra):
        raise GateError("forbidden_paths_invalid")
    forbidden += extra
    for entry in request["changed_files"]:
        if isinstance(entry, str):
            # A plain path cannot attest whether a newly added file is binary.
            raise GateError("changed_files_invalid")
        if not isinstance(entry, dict) or type(entry.get("binary")) is not bool:
            raise GateError("changed_files_invalid")
        if entry.get("status") not in ("added", "modified", "deleted", "renamed"):
            raise GateError("changed_files_invalid")
        candidates = [safe_path(entry.get("path"))]
        if entry["status"] == "renamed":
            candidates.append(safe_path(entry.get("old_path")))
        paths.extend(candidates)
        if request["untrusted"]:
            if entry["binary"] and entry["status"] in ("added", "renamed"):
                raise GateError("forbidden_path")
            for path in candidates:
                basename = PurePosixPath(path).name
                if any(matches(path, p) or fnmatch.fnmatchcase(basename, p) for p in forbidden):
                    raise GateError("forbidden_path")
    return paths


def hosts(value):
    if not isinstance(value, list) or len(value) > 64:
        raise GateError("outbound_allowlist_invalid")
    result = []
    import re
    for host in value:
        if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) or ".." in host:
            raise GateError("outbound_allowlist_invalid")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host.replace(".", "").isdigit():
                raise GateError("outbound_allowlist_invalid")
        else:
            raise GateError("outbound_allowlist_invalid")
        result.append(host)
    return sorted(set(result))


def check_prepared(invocation, report, *, untrusted):
    if not isinstance(report, dict) or report.get("available") is not True:
        raise GateError("verify_unavailable")
    if report.get("sha") != invocation["subject_sha"] or report.get("job_id") != invocation["job_id"]:
        raise GateError("revision_unconfirmed")
    if report.get("environment") != invocation["environment"]:
        raise GateError("environment_unconfirmed")
    if invocation["environment"].startswith("env:") and report.get("entry_verified") is not True:
        raise GateError("verify_unavailable")
    if not untrusted:
        return
    required = ("isolation_verified", "home_absent", "credentials_absent", "host_config_absent", "network_enforced")
    if any(report.get(field) is not True for field in required):
        raise GateError("isolation_unconfirmed")
    if report.get("network_mode") != ("allowlist" if invocation["stage"] == "dependency" else "none"):
        raise GateError("network_unconfirmed")
    if report.get("allow_hosts") != invocation["allow_hosts"]:
        raise GateError("network_unconfirmed")

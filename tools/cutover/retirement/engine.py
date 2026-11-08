"""Stage-4 operator helpers (S-24), with no default command executor.

All operating-system commands go through Context.execute. The caller owns go,
cutover lease validation, append-only events and deduplicated ToDo creation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import logging
from functools import wraps
from pathlib import Path, PurePosixPath as P
import re
import shlex
import time
from typing import Callable, Mapping, Sequence

from . import fsops

try:
    from .site import (
        USER_GROUPS,
        SYSTEM_GROUPS,
        X_GROUPS,
        GH_SYSTEM_UNITS,
        SITE_GHAUTO_LAB_PRUNE_SERVICE,
        SITE_GH_AUTO_SERVICE,
        SITE_RELEASE_BOTH,
        SITE_GHAUTO_LAB_PRUNE_TIMER,
        SITE_GH_AUTO_TIMER,
        SITE_GH_AUTO_OLD,
        SITE_GH_AUTO,
        SITE_GH_AUTO_TASKS,
        SITE_GH_AUTO_REVIEW_PR4_GPT,
        SITE_GH_AUTO_9,
        SITE_GHAUTO_LAB_RUNNER_PRUNE,
        SITE_GHAUTO_LAB_PRUNE,
        SITE_STOP_ARCHIVE_TO_HDD,
        SITE_T_78_ARCHIVE_TO_HDD,
        SITE_ARCHIVE_TO_HDD_RUNNING,
        SITE_ARCHIVE_TO_HDD,
        SITE_GH_AUTO_OLD_G9,
        SITE_GH_AUTO_17,
        SITE_CLAUDE_GH_AUTO_LOCKS_LOCK,
        SITE_CLAUDE_GH_AUTO_STATE_JSON_POLL_LOCK,
        SITE_GHAUTO_SBX_NFT,
        SITE_WEBSBX_NFT,
        SITE_GH_AUTO_BAK,
        SITE_GHAUTO_LAB_RUNNER,
        SITE_GHAUTO_LAB_SETUP,
        SITE_GHAUTO_REMOTE_TEST,
        SITE_CLAUDE_BIN,
        SITE_SUDOERS_D_WEBSBX_IBOX,
        SITE_ARCHIVE_TO_HDD_TIMER,
        SITE_GH_AUTO_OLD_G10,
        SITE_AX_CUTOVER_MOVE_GH_AUTO_SCRIPTS_TO_ARCHIVE,
        SITE_AX_ENV_CI_DATA_SOD_GAMEDATA,
        SITE_AX_ENV_CI_NUGET,
        SITE_WEBSBX_IBOX,
        SITE_CACHE_GH_AUTO,
        SITE_CACHE_GH_AUTO_35,
        SITE_EIGENFLUX_HEARTBEAT,
        SITE_ARCHIVE_TO_HDD_SERVICE,
        SITE_NAME_GHAUTO_LAB,
        SITE_GHAUTO_LAB_JOBS,
        SITE_GHAUTO_LAB_DATA_SOD_GAMEDATA,
        SITE_GHAUTO_LAB_NUGET,
        SITE_GHAUTO_LAB_ARCHIVE,
        SITE_GHAUTO_LAB_TEST_RESULTS,
        SITE_GHAUTO_LAB,
        SITE_GHAUTO_SBX_NFT_REFRESH,
        SITE_GHAUTO_SBX_NFT_REFRESH_BAK,
        SITE_WEBSBX_NFT_APPLY,
        SITE_CLAUDE_WORK,
        SITE_CLAUDE_GH_AUTO,
        SITE_STATE_GH_AUTO,
        SITE_UNITS_SYSTEM_SUDOERS_WEBSBX_IBOX,
        SITE_AX_ENV_ARCHIVE,
        SITE_GH_AUTO_OLD_53,
        SITE_ARCHIVE_TO_HDD_54,
        SITE_GHAUTO_OLD,
    )
    from . import site as _site
    _site_compute_role = getattr(_site, "COMPUTE_ROLE", "compute")
except ModuleNotFoundError as exc:
    if exc.name != f"{__package__}.site":
        raise
    import runpy
    import warnings

    warnings.warn("retirement site.py is missing; using site.example.py", RuntimeWarning, stacklevel=2)
    _site_example = runpy.run_path(str(Path(__file__).with_name("site.example.py")))
    _site_compute_role = _site_example.get("COMPUTE_ROLE", "compute")
    globals().update({name: _site_example[name] for name in ('USER_GROUPS', 'SYSTEM_GROUPS', 'X_GROUPS', 'GH_SYSTEM_UNITS', 'SITE_GHAUTO_LAB_PRUNE_SERVICE', 'SITE_GH_AUTO_SERVICE', 'SITE_RELEASE_BOTH', 'SITE_GHAUTO_LAB_PRUNE_TIMER', 'SITE_GH_AUTO_TIMER', 'SITE_GH_AUTO_OLD', 'SITE_GH_AUTO', 'SITE_GH_AUTO_TASKS', 'SITE_GH_AUTO_REVIEW_PR4_GPT', 'SITE_GH_AUTO_9', 'SITE_GHAUTO_LAB_RUNNER_PRUNE', 'SITE_GHAUTO_LAB_PRUNE', 'SITE_STOP_ARCHIVE_TO_HDD', 'SITE_T_78_ARCHIVE_TO_HDD', 'SITE_ARCHIVE_TO_HDD_RUNNING', 'SITE_ARCHIVE_TO_HDD', 'SITE_GH_AUTO_OLD_G9', 'SITE_GH_AUTO_17', 'SITE_CLAUDE_GH_AUTO_LOCKS_LOCK', 'SITE_CLAUDE_GH_AUTO_STATE_JSON_POLL_LOCK', 'SITE_GHAUTO_SBX_NFT', 'SITE_WEBSBX_NFT', 'SITE_GH_AUTO_BAK', 'SITE_GHAUTO_LAB_RUNNER', 'SITE_GHAUTO_LAB_SETUP', 'SITE_GHAUTO_REMOTE_TEST', 'SITE_CLAUDE_BIN', 'SITE_SUDOERS_D_WEBSBX_IBOX', 'SITE_ARCHIVE_TO_HDD_TIMER', 'SITE_GH_AUTO_OLD_G10', 'SITE_AX_CUTOVER_MOVE_GH_AUTO_SCRIPTS_TO_ARCHIVE', 'SITE_AX_ENV_CI_DATA_SOD_GAMEDATA', 'SITE_AX_ENV_CI_NUGET', 'SITE_WEBSBX_IBOX', 'SITE_CACHE_GH_AUTO', 'SITE_CACHE_GH_AUTO_35', 'SITE_EIGENFLUX_HEARTBEAT', 'SITE_ARCHIVE_TO_HDD_SERVICE', 'SITE_NAME_GHAUTO_LAB', 'SITE_GHAUTO_LAB_JOBS', 'SITE_GHAUTO_LAB_DATA_SOD_GAMEDATA', 'SITE_GHAUTO_LAB_NUGET', 'SITE_GHAUTO_LAB_ARCHIVE', 'SITE_GHAUTO_LAB_TEST_RESULTS', 'SITE_GHAUTO_LAB', 'SITE_GHAUTO_SBX_NFT_REFRESH', 'SITE_GHAUTO_SBX_NFT_REFRESH_BAK', 'SITE_WEBSBX_NFT_APPLY', 'SITE_CLAUDE_WORK', 'SITE_CLAUDE_GH_AUTO', 'SITE_STATE_GH_AUTO', 'SITE_UNITS_SYSTEM_SUDOERS_WEBSBX_IBOX', 'SITE_AX_ENV_ARCHIVE', 'SITE_GH_AUTO_OLD_53', 'SITE_ARCHIVE_TO_HDD_54', 'SITE_GHAUTO_OLD')})


COMPUTE_ROLE = os.environ.get("AX_COMPUTE_ROLE") or _site_compute_role or "compute"


PRECONDITIONS = (
    "build_complete", "removal_go_verified", "originals_copied", "new_entry_checked",
    "old_entry_removed", "idle_checked", "values_registered", "monitoring_ok",
    "gh_config_registered", "dockerfiles_copied",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class Host:
    """Values come from the installation profile, never the controller's HOME."""
    name: str
    home: str
    user_units: str
    system_units: str
    sbin: str
    fstab: str
    bind_mount: str
    runtime_dir: str
    uid: int
    disk_root: str | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,253}", self.name):
            raise ValueError("invalid-host")
        for name in ("home", "user_units", "system_units", "sbin", "fstab", "bind_mount", "runtime_dir"):
            value = getattr(self, name)
            if not P(value).is_absolute() or ".." in P(value).parts:
                raise ValueError("absolute-profile-path-required")
        if self.disk_root is not None and (not P(self.disk_root).is_absolute() or ".." in P(self.disk_root).parts):
            raise ValueError("absolute-disk-root-required")

    def archive(self, day: str) -> str:
        return str(P(self.home) / ".local/share/ax/archive" / f"ax-cutover-{day}")


@dataclass(frozen=True)
class Profile:
    local: Host
    legacy: Host
    compute: Host
    day: str
    preconditions: Mapping[str, bool]
    optional_unmount: bool = False
    wait_seconds: float = 1800
    poll_seconds: float = 60

    def __post_init__(self):
        if not re.fullmatch(r"\d{8}", self.day):
            raise ValueError("go-directory-date-required")
        datetime.strptime(self.day, "%Y%m%d")
        if len({self.local.name, self.legacy.name, self.compute.name}) != 3:
            raise ValueError("three-distinct-profile-hosts-required")
        if not self.compute.disk_root:
            raise ValueError("compute-disk-root-required")
        if self.wait_seconds < 0 or self.poll_seconds <= 0:
            raise ValueError("invalid-wait-policy")


# Normalize legacy keyword inputs at the public Profile constructor.
_profile_init = Profile.__init__


@wraps(_profile_init)
def _profile_init_with_legacy_keys(self, *args, **kwargs):
    keys = (key.strip() for key in os.environ.get("AX_COMPUTE_LEGACY_KEYS", "").split(","))
    for key in keys:
        if not key or key == "compute" or key not in kwargs:
            continue
        value = kwargs.pop(key)
        if "compute" in kwargs or len(args) > 2:
            current = kwargs.get("compute") if "compute" in kwargs else args[2]
            if value != current:
                logging.getLogger(__name__).warning("Conflicting legacy compute profile key ignored")
        else:
            kwargs["compute"] = value
    _profile_init(self, *args, **kwargs)


Profile.__init__ = _profile_init_with_legacy_keys


def unverified_noded_replacement(_host: Host) -> bool:
    """Keep compute-host payloads in place until B15/B17 supplies T-142 evidence."""
    return False


@dataclass
class Context:
    # execute(host, argv, stdin): common.py's runner is adapted here by the caller.
    execute: Callable[[str, Sequence[str], str | None], CommandResult]
    # record(kind, subject, data): common.py's claim-checked append-only recorder.
    record: Callable[[str, str, dict], None]
    latest: Callable[[str], dict | None]
    card: Callable[[str, str, str], None]
    # Must check the live go/lease before commands; never a caller-supplied token.
    authorize: Callable[[], None]
    # B15/B17's actual T-142 check supplies this verdict. No invented shared
    # event subject, schema, or evidence-file name is assumed for T-142.
    verify_noded_replacement: Callable[[Host], bool] = unverified_noded_replacement
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    utc: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class RetirementResult:
    steps: dict[tuple[str, str], str] = field(default_factory=dict)
    stopped: dict[tuple[str, str, str], bool] = field(default_factory=dict)
    condition1: bool = False
    archive_to_hdd_stopped: bool = False
    # Retirement does not resume loops or enable reap. The stage coordinator
    # checks conditions 2..5, including condition3, and invokes b17-3 once.
    next_step: str = "collect_only"


class StepFailure(RuntimeError):
    pass


class Waiting(StepFailure):
    pass


def _retirement_archive_name() -> str:
    name = os.environ.get("AX_RETIREMENT_ARCHIVE_NAME")
    if name is None and os.environ.get("AX_HOME"):
        config = Path(os.environ["AX_HOME"]) / "config" / "launcher.env"
        if config.is_file():
            matches = []
            for line in config.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == "AX_RETIREMENT_ARCHIVE_NAME":
                    parts = shlex.split(value, comments=True)
                    if len(parts) != 1:
                        raise ValueError("invalid-retirement-archive-name")
                    matches.append(parts[0])
            if len(matches) > 1:
                raise ValueError("invalid-retirement-archive-name")
            if matches:
                name = matches[0]
    name = SITE_GH_AUTO_OLD if name is None else name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise ValueError("invalid-retirement-archive-name")
    return name


class Executor:
    def __init__(self, context: Context, profile: Profile):
        self.c = context
        self.p = profile
        self.r = RetirementResult()
        self.blocked: set[str] = set()
        self.unreachable: set[str] = set()
        self.program = Path(fsops.__file__).read_text(encoding="utf-8")
        self.baseline: dict[str, str] = {}
        self.archive_name = _retirement_archive_name()

    def command(self, h: Host, argv: Sequence[str], stdin: str | None = None,
                allowed: tuple[int, ...] = (0,)) -> CommandResult:
        self.c.authorize()
        try:
            result = self.c.execute(h.name, tuple(argv), stdin)
        except (OSError, ConnectionError, TimeoutError) as exc:
            self.unreachable.add(h.name)
            raise StepFailure("unreachable") from exc
        if result.returncode == 255:
            self.unreachable.add(h.name)
            raise StepFailure("unreachable")
        if result.returncode not in allowed:
            raise StepFailure("command-failed")
        return result

    def files(self, h: Host, op: str, **kw) -> dict:
        payload = {"op": op, "archive": h.archive(self.p.day), **kw}
        protected = (h.system_units, h.sbin, h.fstab, str(P(h.system_units).parent.parent / "sudoers.d"))
        paths = list(kw.get("patterns", [])) + [x for pair in kw.get("moves", []) for x in pair]
        elevated = op in ("prepare", "verify", "fstab") or any(
            path == base or path.startswith(base + "/") for path in paths for base in protected
        )
        prefix = ("sudo", "-n") if elevated else ()
        env = ("env", f"AX_RETIREMENT_ARCHIVE_NAME={self.archive_name}")
        result = self.command(h, (*prefix, *env, "python3", "-I", "-c", self.program), json.dumps(payload))
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise StepFailure("file-result-invalid") from exc
        return value

    def systemctl(self, h: Host, scope: str, *args: str) -> CommandResult:
        prefix = ("env", f"XDG_RUNTIME_DIR={h.runtime_dir}", "systemctl", "--user") if scope == "user" else ("sudo", "-n", "systemctl")
        return self.command(h, (*prefix, *args), allowed=(0, 1, 3, 4, 5))

    def inactive(self, h: Host, scope: str, unit: str) -> bool:
        state = self.systemctl(h, scope, "is-active", unit).stdout.strip()
        return state in ("inactive", "failed", "unknown")

    def disabled(self, h: Host, scope: str, unit: str) -> bool:
        state = self.systemctl(h, scope, "is-enabled", unit).stdout.strip()
        if state in ("disabled", "static", "not-found"):
            return True
        if state:
            return False
        # systemctl may put ENOENT only on stderr. Confirm LoadState instead of
        # interpreting an arbitrary failing/empty command as a stopped unit.
        return self.systemctl(h, scope, "show", unit, "-p", "LoadState", "--value").stdout.strip() == "not-found"

    def stop(self, h: Host, scope: str, units: Sequence[str]) -> None:
        # static service disable may warn: observed state, not exit code, decides.
        self.systemctl(h, scope, "disable", "--now", *units)
        if not all(self.inactive(h, scope, u) and self.disabled(h, scope, u) for u in units):
            raise StepFailure("unit-still-active-or-enabled")
        timers = self.systemctl(h, scope, "list-timers", "--all", "--no-pager").stdout.splitlines()
        if any(u.endswith(".timer") and any(u in line.split() for line in timers) for u in units):
            raise StepFailure("timer-still-listed")

    def event(self, h: Host, subject: str, result: str, reason: str | None = None) -> None:
        data = {"target": f"{subject.rsplit('/', 1)[-1]}/{h.name}", "result": result}
        if reason:
            data["reason"] = reason
        self.c.record("cutover.state", subject, data)

    def card(self, h: Host, step: str, reason: str) -> None:
        self.c.card(f"retirement/{h.name}/{step}", f"旧部品の撤去を確認する: {step}",
                    f"対象 {h.name}。理由 {reason}。該当手順を確認して再実行する。実行中の処理は無理に止めない。")

    def step(self, h: Host, g: str, action: Callable[[], None], *, require: str | None = None) -> None:
        reason = None
        if h.name in self.blocked:
            # An unreachable compute host must explicitly retain G3 failed evidence.
            result = "failed" if g == "G3" and h.name in self.unreachable else "skipped"
            reason = "unreachable" if h.name in self.unreachable else "dependency"
        elif require and self.r.steps.get((require, h.name)) != "ok":
            result, reason = "skipped", "dependency"
        else:
            try:
                action()
                result = "ok"
            except Waiting as exc:
                result, reason = "waiting", str(exc)
                self.blocked.add(h.name)
                self.card(h, g, reason)
            except StepFailure as exc:
                result, reason = "failed", str(exc)
                self.blocked.add(h.name)
                self.card(h, g, reason)
        self.r.steps[g, h.name] = result
        self.event(h, f"{SITE_GH_AUTO_OLD_53}{g}", result, reason)

    def poll(self, h: Host, name: str, predicate: Callable[[], bool]) -> None:
        end = self.c.monotonic() + self.p.wait_seconds
        while not predicate():
            left = end - self.c.monotonic()
            if left <= 0:
                raise Waiting(name)
            self.c.sleep(min(self.p.poll_seconds, left))

    def process_absent(self, h: Host, token: str) -> bool:
        rows = self.command(h, ("ps", "-eo", "args")).stdout.splitlines()
        return not any(token in line for line in rows)

    def idle(self, h: Host, role: str) -> bool:
        unit = SITE_GHAUTO_LAB_PRUNE_SERVICE if role == COMPUTE_ROLE else SITE_GH_AUTO_SERVICE
        if not self.inactive(h, "user", unit):
            return False
        if role == COMPUTE_ROLE:
            names = self.command(h, ("docker", "ps", "--filter", SITE_NAME_GHAUTO_LAB, "--format", "{{.Names}}")).stdout.strip()
            busy = self.files(h, "busy", jobs=str(P(h.home) / SITE_GHAUTO_LAB_JOBS))["busy"]
            return not names and not busy and self.process_absent(h, SITE_GHAUTO_LAB_RUNNER_PRUNE)
        if role == "legacy":
            return self.process_absent(h, SITE_RELEASE_BOTH)
        locks = [str(P(h.home) / SITE_CLAUDE_GH_AUTO_LOCKS_LOCK), str(P(h.home) / SITE_CLAUDE_GH_AUTO_STATE_JSON_POLL_LOCK)]
        if self.files(h, "busy", lock_patterns=locks)["busy"] or not self.process_absent(h, SITE_RELEASE_BOTH):
            return False
        # A disconnected legacy host does not stop another host's independent Gs.
        if self.p.legacy.name not in self.unreachable:
            if not self.process_absent(self.p.legacy, SITE_RELEASE_BOTH):
                return False
        return True

    def unit_moves(self, h: Host, scope: str, names: Sequence[str]) -> list[list[str]]:
        base = h.user_units if scope == "user" else h.system_units
        patterns = [str(P(base) / p) for n in names for p in (n, n + ".d", n + ".bak-*")]
        paths = self.files(h, "glob", patterns=patterns)["paths"]
        return [[p, str(P(h.archive(self.p.day)) / "units" / scope / P(p).name)] for p in paths]

    def move(self, h: Host, moves: list[list[str]]) -> None:
        self.files(h, "move", host=h.name, moves=moves, at=self.c.utc())
        for source, _ in moves:
            self.c.record("removal.state", source, {"component_id": source, "result": "ok"})

    def gh_units(self, h: Host, role: str) -> None:
        names = (SITE_GHAUTO_LAB_PRUNE_SERVICE, SITE_GHAUTO_LAB_PRUNE_TIMER) if role == COMPUTE_ROLE else (SITE_GH_AUTO_SERVICE, SITE_GH_AUTO_TIMER)
        moves = self.unit_moves(h, "user", names)
        if role != COMPUTE_ROLE:
            moves += self.unit_moves(h, "system", GH_SYSTEM_UNITS)
        self.move(h, moves)
        for scope in (("user",) if role == COMPUTE_ROLE else ("user", "system")):
            if self.systemctl(h, scope, "daemon-reload").returncode:
                raise StepFailure("daemon-reload-failed")
        if self.systemctl(h, "user", "reset-failed").returncode:
            raise StepFailure("reset-failed")
        self.check_unit_listing(h, role)
        for scope, group in (("user", SITE_GHAUTO_LAB_PRUNE),) if role == COMPUTE_ROLE else (("user", SITE_GH_AUTO), ("system", SITE_GHAUTO_SBX_NFT), ("system", SITE_WEBSBX_NFT)):
            self.r.stopped[h.name, scope, group] = True
            self.event(h, f"stop-{group}", "ok")

    def payload_moves(self, h: Host, role: str) -> list[list[str]]:
        home, old = P(h.home), P(h.archive(self.p.day)) / self.archive_name
        if role == COMPUTE_ROLE:
            disk = P(h.disk_root)
            return [[str(a), str(b)] for a, b in (
                (home / SITE_GHAUTO_LAB_DATA_SOD_GAMEDATA, home / SITE_AX_ENV_CI_DATA_SOD_GAMEDATA),
                (disk / SITE_GHAUTO_LAB_NUGET, disk / SITE_AX_ENV_CI_NUGET),
                (disk / SITE_GHAUTO_LAB_ARCHIVE, disk / SITE_AX_ENV_ARCHIVE / f"{SITE_GHAUTO_OLD}{self.p.day}" / "archive"),
                (disk / SITE_GHAUTO_LAB_TEST_RESULTS, disk / SITE_AX_ENV_ARCHIVE / f"{SITE_GHAUTO_OLD}{self.p.day}" / "test-results"),
                (home / SITE_GHAUTO_LAB, old / SITE_GHAUTO_LAB),
            )]
        patterns = [str(home / SITE_CLAUDE_BIN / name) for name in (SITE_GH_AUTO, SITE_GH_AUTO_BAK, SITE_GHAUTO_LAB_RUNNER, SITE_GHAUTO_LAB_SETUP, SITE_GHAUTO_REMOTE_TEST)]
        binpaths = self.files(h, "glob", patterns=patterns)["paths"]
        sbinpaths = self.files(h, "glob", patterns=[str(P(h.sbin) / n) for n in (SITE_GHAUTO_SBX_NFT_REFRESH, SITE_GHAUTO_SBX_NFT_REFRESH_BAK, SITE_WEBSBX_NFT_APPLY)])["paths"]
        work_names = (SITE_GH_AUTO, SITE_GH_AUTO_TASKS, SITE_GH_AUTO_REVIEW_PR4_GPT) if role == "local" else (SITE_GH_AUTO_9,)
        work = self.files(h, "glob", patterns=[str(home / SITE_CLAUDE_WORK / name) for name in work_names])["paths"]
        return ([[p, str(old / "bin" / P(p).name)] for p in binpaths]
                + [[p, str(old / "sbin" / P(p).name)] for p in sbinpaths]
                + [[str(home / SITE_CLAUDE_GH_AUTO), str(old / SITE_STATE_GH_AUTO)]]
                + [[p, str(old / "work" / P(p).name)] for p in work]
                + [[str(home / SITE_CACHE_GH_AUTO), str(old / SITE_CACHE_GH_AUTO_35)]])

    def noded_verified(self, h: Host) -> bool:
        self.c.authorize()
        try:
            return self.c.verify_noded_replacement(h) is True
        except (NotImplementedError, OSError, StepFailure):
            return False

    def payload(self, h: Host, role: str) -> None:
        if role == COMPUTE_ROLE and not self.noded_verified(h):
            raise StepFailure("t142")
        moves = self.payload_moves(h, role)
        self.move(h, moves)
        if role != COMPUTE_ROLE:
            repo = str(P(h.home) / SITE_CLAUDE_BIN)
            # Only tracked moved script paths, never other operators' changes.
            archived = self.files(h, "verify")["components"]
            names = sorted({P(a).name for a in archived if str(P(a).parent) == repo})
            if not names:
                return
            tracked = self.command(h, ("git", "-C", repo, "ls-files", "--", *names)).stdout.splitlines()
            if tracked:
                self.command(h, ("git", "-C", repo, "add", "-A", "--", *tracked))
                self.command(h, ("git", "-C", repo, "commit", "-m", SITE_AX_CUTOVER_MOVE_GH_AUTO_SCRIPTS_TO_ARCHIVE, "--", *tracked))

    def stop_gh(self, h: Host, role: str) -> None:
        archive = P(h.archive(self.p.day))
        rules = self.command(h, ("sudo", "-n", "nft", "list", "ruleset")).stdout
        stamp = re.sub(r"[^0-9]", "", self.c.utc())
        self.files(h, "save", path=str(archive / "nft" / f"ruleset-{stamp}.txt"), text=rules)
        if role == "local":
            self.stop(h, "user", (SITE_GH_AUTO_SERVICE,))
        elif not self.inactive(h, "user", SITE_GH_AUTO_SERVICE) or not self.disabled(h, "user", SITE_GH_AUTO_TIMER):
            raise StepFailure("legacy-user-unit-active")
        self.stop(h, "system", GH_SYSTEM_UNITS)

    def g1(self, h: Host, role: str) -> None:
        if role == "legacy":
            if not self.disabled(h, "user", SITE_GH_AUTO_TIMER) or not self.inactive(h, "user", SITE_GH_AUTO_TIMER):
                raise StepFailure("legacy-timer-active")
        else:
            self.stop(h, "user", ((SITE_GHAUTO_LAB_PRUNE if role == COMPUTE_ROLE else SITE_GH_AUTO) + ".timer",))

    def g3(self) -> None:
        h = self.p.compute
        self.stop(h, "user", (SITE_GHAUTO_LAB_PRUNE_TIMER, SITE_GHAUTO_LAB_PRUNE_SERVICE))
        if not self.process_absent(h, SITE_GHAUTO_LAB_RUNNER_PRUNE):
            raise StepFailure("prune-running")

    def g10(self, h: Host) -> None:
        if not self.p.optional_unmount:
            return
        self.files(h, "fstab", path=h.fstab, host=h.name)
        self.command(h, ("sudo", "-n", "findmnt", "--verify", "--tab-file", h.fstab))
        result = self.command(h, ("sudo", "-n", "umount", h.bind_mount), allowed=(0, 1, 32))
        if result.returncode:
            self.c.record("removal.check", "bind-mount", {"component_id": f"bind-mount/{h.name}", "result": "skipped", "reason": "busy-left-mounted"})

    def check_unit_listing(self, h: Host, role: str) -> None:
        groups = (SITE_GHAUTO_LAB_PRUNE,) if role == COMPUTE_ROLE else (SITE_GH_AUTO,)
        for verb in ("list-units", "list-timers"):
            text = self.systemctl(h, "user", verb, "--all", "--no-pager").stdout
            tokens = text.split()
            if any(g + suffix in tokens for g in groups for suffix in (".service", ".timer")):
                raise StepFailure("old-unit-listed")
        if role != COMPUTE_ROLE:
            text = self.systemctl(h, "system", "list-units", "--all", "--no-pager").stdout
            if any(unit in text.split() for unit in GH_SYSTEM_UNITS):
                raise StepFailure("old-system-unit-listed")

    def g11(self, h: Host, role: str) -> None:
        required = []
        if role == COMPUTE_ROLE and self.r.steps.get(("G9", h.name)) == "ok":
            required = [str(P(h.home) / SITE_AX_ENV_CI_DATA_SOD_GAMEDATA), str(P(h.disk_root) / SITE_AX_ENV_CI_NUGET)]
        checks = (
            ("unit-listings", lambda: self.check_unit_listing(h, role)),
            ("manifest", lambda: self.files(h, "verify", required=required)),
            ("containers", lambda: self.check_containers(h)),
        )
        for name, check in checks:
            try:
                check()
            except StepFailure:
                self.c.record("removal.check", name, {"component_id": f"{name}/{h.name}", "result": "failed"})
                raise
            self.c.record("removal.check", name, {"component_id": f"{name}/{h.name}", "result": "ok"})

    def check_containers(self, h: Host) -> None:
        current = self.command(h, ("docker", "ps", "--format", "{{.ID}} {{.Names}}" )).stdout
        if sorted(current.splitlines()) != sorted(self.baseline[h.name].splitlines()):
            raise StepFailure("retained-containers-changed")

    def generic(self, h: Host, group: str) -> None:
        self.poll(h, f"busy-{group}", lambda: self.inactive(h, "user", group + ".service"))
        self.stop(h, "user", (group + ".service",))
        moves = self.unit_moves(h, "user", (group + ".service", group + ".timer"))
        self.move(h, moves)
        if self.systemctl(h, "user", "daemon-reload").returncode:
            raise StepFailure("daemon-reload-failed")
        if self.systemctl(h, "user", "reset-failed").returncode:
            raise StepFailure("reset-failed")

    def stop_remaining(self) -> None:
        h = self.p.local
        # Only this host's failed gh sequence blocks these local operations.
        if h.name in self.blocked:
            return
        remaining = [g for g in USER_GROUPS if g != SITE_GH_AUTO]
        timer_failures = set()
        for group in remaining:
            try:
                self.stop(h, "user", (group + ".timer",))
            except StepFailure as exc:
                timer_failures.add(group)
                self.r.stopped[h.name, "user", group] = False
                self.event(h, f"stop-{group}", "failed", str(exc))
                self.card(h, group, str(exc))
        for group in remaining:
            if group in timer_failures:
                continue
            try:
                self.generic(h, group)
                self.r.stopped[h.name, "user", group] = True
                self.event(h, f"stop-{group}", "ok")
            except StepFailure as exc:
                self.r.stopped[h.name, "user", group] = False
                self.event(h, f"stop-{group}", "waiting" if isinstance(exc, Waiting) else "failed", str(exc))
                self.card(h, group, str(exc))
        # Old sudo permission goes only after all its known consumers are stopped.
        if all(self.r.stopped.get((h.name, scope, group)) for scope, group in (("system", SITE_GHAUTO_SBX_NFT), ("system", SITE_WEBSBX_NFT), ("user", SITE_EIGENFLUX_HEARTBEAT))):
            sudoers = str(P(h.system_units).parent.parent / SITE_SUDOERS_D_WEBSBX_IBOX)
            try:
                self.move(h, [[sudoers, str(P(h.archive(self.p.day)) / SITE_UNITS_SYSTEM_SUDOERS_WEBSBX_IBOX)]])
            except StepFailure as exc:
                self.card(h, SITE_WEBSBX_IBOX, str(exc))

    def archive_last(self) -> None:
        h = self.p.local
        if latest_result(self.c, "T-78") != "ok":
            self.event(h, SITE_STOP_ARCHIVE_TO_HDD, "skipped", "t78")
            self.c.card("retirement/t78", SITE_T_78_ARCHIVE_TO_HDD,
                        "退避の内容照合が未達。T-78 を直して結果を記録し、停止集合と条件3を確かめ直す。巡回と元の除去は再開しない。")
            return
        if h.name in self.blocked:
            self.event(h, SITE_STOP_ARCHIVE_TO_HDD, "skipped", "dependency")
            return
        try:
            lock = str(P(h.runtime_dir) / f"{SITE_ARCHIVE_TO_HDD_54}{h.uid}.lock")
            self.poll(h, SITE_ARCHIVE_TO_HDD_RUNNING, lambda: self.inactive(h, "user", SITE_ARCHIVE_TO_HDD_SERVICE) and not self.files(h, "busy", lock_patterns=[lock])["busy"])
            self.stop(h, "user", (SITE_ARCHIVE_TO_HDD_TIMER,))
            self.generic(h, SITE_ARCHIVE_TO_HDD)
            self.r.archive_to_hdd_stopped = True
            self.r.stopped[h.name, "user", SITE_ARCHIVE_TO_HDD] = True
            self.event(h, SITE_STOP_ARCHIVE_TO_HDD, "ok")
        except StepFailure as exc:
            self.event(h, SITE_STOP_ARCHIVE_TO_HDD, "waiting" if isinstance(exc, Waiting) else "failed", str(exc))
            self.card(h, SITE_ARCHIVE_TO_HDD, str(exc))

    def verify_set(self) -> bool:
        expected = stop_set(self.p)
        complete = True
        for name, scope, group in expected:
            if not self.r.stopped.get((name, scope, group), False):
                complete = False
                continue
            h = next(h for h in (self.p.local, self.p.legacy, self.p.compute) if h.name == name)
            try:
                for unit in (group + ".service", group + ".timer"):
                    if not self.inactive(h, scope, unit) or not self.disabled(h, scope, unit):
                        complete = False
                if self.unit_moves(h, scope, (group + ".service", group + ".timer")):
                    complete = False
            except StepFailure:
                complete = False
        for h in (self.p.local, self.p.legacy, self.p.compute):
            if h.name in self.blocked:
                continue
            try:
                self.files(h, "verify")
                self.check_containers(h)
                if h == self.p.local:
                    text = self.command(h, ("ss", "-ltn")).stdout
                    ports = {3450, 3461, 3462, 3464, 3466}
                    for line in text.splitlines():
                        fields = line.split()
                        if len(fields) >= 4 and fields[0] == "LISTEN":
                            value = fields[3].rsplit(":", 1)[-1]
                            if value.isdigit() and int(value) in ports:
                                raise StepFailure("old-port-listening")
            except StepFailure as exc:
                complete = False
                self.card(h, "stop-set", str(exc))
                self.c.record("removal.check", "stop-set", {"component_id": f"stop-set/{h.name}", "result": "failed", "reason": str(exc)})
            else:
                self.c.record("removal.check", "stop-set", {"component_id": f"stop-set/{h.name}", "result": "ok"})
        return complete

    def run(self, *, stop_archiver: bool = True) -> RetirementResult:
        self.c.authorize()
        missing = [key for key in PRECONDITIONS if self.p.preconditions.get(key) is not True]
        if missing:
            raise StepFailure("stage4-preconditions:" + ",".join(missing))
        roles = ((self.p.compute, COMPUTE_ROLE), (self.p.local, "local"), (self.p.legacy, "legacy"))
        # An already-held release/poll lock invalidates stage-4 admission before
        # even G1 changes timers. A disconnected host remains a local failure.
        try:
            locks = [str(P(self.p.local.home) / SITE_CLAUDE_GH_AUTO_LOCKS_LOCK),
                     str(P(self.p.local.home) / SITE_CLAUDE_GH_AUTO_STATE_JSON_POLL_LOCK)]
            busy = self.files(self.p.local, "busy", lock_patterns=locks)["busy"]
        except StepFailure:
            busy = False
        if busy:
            self.card(self.p.local, "admission", "work-still-running")
            raise Waiting("work-still-running")
        # Read-only preparations precede G1; no timer or service changes here.
        for h, role in roles:
            try:
                self.baseline[h.name] = self.command(h, ("docker", "ps", "--format", "{{.ID}} {{.Names}}")).stdout
            except StepFailure:
                pass  # G1 records the host failure, without blocking others.
        for h, role in roles:
            self.step(h, "G1", lambda h=h, role=role: self.g1(h, role))
        # Probe legacy first so a lost legacy host never blocks a local check.
        for h, role in (roles[2], roles[0], roles[1]):
            self.step(h, "G2", lambda h=h, role=role: self.poll(h, "work-still-running", lambda: self.idle(h, role)), require="G1")
        self.step(self.p.compute, "G3", self.g3, require="G2")
        self.step(self.p.local, "G4", lambda: self.stop_gh(self.p.local, "local"), require="G2")
        self.step(self.p.legacy, "G5", lambda: self.stop_gh(self.p.legacy, "legacy"), require="G2")
        for h, role in roles:
            def archive_units(h=h, role=role):
                self.files(h, "prepare")
                self.files(h, "preflight", moves=self.payload_moves(h, role))
                self.gh_units(h, role)
            self.step(h, "G6", archive_units, require={COMPUTE_ROLE: "G3", "local": "G4", "legacy": "G5"}[role])
        self.step(self.p.local, "G7", lambda: self.payload(self.p.local, "local"), require="G6")
        self.step(self.p.legacy, "G8", lambda: self.payload(self.p.legacy, "legacy"), require="G6")
        if self.p.compute.name not in self.blocked and not self.noded_verified(self.p.compute):
            self.r.steps["G9", self.p.compute.name] = "skipped"
            self.event(self.p.compute, SITE_GH_AUTO_OLD_G9, "skipped", "t142")
            self.card(self.p.compute, "G9", "t142")
        else:
            self.step(self.p.compute, "G9", lambda: self.payload(self.p.compute, COMPUTE_ROLE), require="G6")
        for h in (self.p.local, self.p.legacy):
            if not self.p.optional_unmount and h.name not in self.blocked:
                self.r.steps["G10", h.name] = "skipped"
                self.event(h, SITE_GH_AUTO_OLD_G10, "skipped", "optional")
            else:
                self.step(h, "G10", lambda h=h: self.g10(h), require="G7" if h == self.p.local else "G8")
        for h, role in roles:
            self.step(h, "G11", lambda h=h, role=role: self.g11(h, role))
        self.stop_remaining()
        if stop_archiver:
            self.archive_last()
        self.r.condition1 = self.verify_set()
        if self.r.condition1:
            self.r.next_step = "check-condition3"
        if self.r.steps.get(("G9", self.p.compute.name)) == "ok" and self.r.steps.get(("G8", self.p.legacy.name)) == "ok":
            self.c.card("retirement/retained-workcopies", SITE_GH_AUTO_17, "他ホストの退避済み作業コピーを確認する。AX は削除しない。")
        return self.r


def latest_result(context: Context, subject: str) -> str | None:
    event = context.latest(subject)
    if not event or event.get("kind") != "cutover.state" or event.get("subject") != subject:
        return None
    return event.get("data", {}).get("result")


def stop_set(profile: Profile) -> tuple[tuple[str, str, str], ...]:
    """Unique targets in stopping order, including conditional archive-to-hdd."""
    return tuple(dict.fromkeys(
        [(profile.local.name, "user", g) for g in (*USER_GROUPS, SITE_ARCHIVE_TO_HDD)]
        + [(profile.local.name, "system", g) for g in SYSTEM_GROUPS]
        + [(profile.legacy.name, "user", SITE_GH_AUTO)]
        + [(profile.legacy.name, "system", g) for g in SYSTEM_GROUPS]
        + [(profile.compute.name, "user", SITE_GHAUTO_LAB_PRUNE)]
    ))


def run_retirement(context: Context, profile: Profile) -> RetirementResult:
    """Called by b17-1 at stage 4; no work starts at import time."""
    return Executor(context, profile).run()

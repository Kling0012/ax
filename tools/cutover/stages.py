"""Six operator stages (F-15). Backend observations must be fresh, not attestations in a plan."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from .common import Context, CutoverError, GateBlocked, IntegrationPending, latest_result

# T-72 and T-75 are retired, never resurrected as runtime conditions.
RETIRED_TESTS = ("T-72", "T-75")
REAL_TESTS = ("T-55", "T-58", "T-78", "T-80", "T-104", "T-106", "T-150", "T-186",
              "T-189", "T-195", "T-209", "T-210", "T-211", "T-215")
try:
    from .stages_site import EXTERNAL_SENDERS
except ModuleNotFoundError as exc:
    if exc.name != f"{__package__}.stages_site":
        raise
    import runpy
    import warnings
    from pathlib import Path

    warnings.warn("stages_site.py is missing; using stages_site.example.py", RuntimeWarning, stacklevel=2)
    EXTERNAL_SENDERS = runpy.run_path(str(Path(__file__).with_name("stages_site.example.py")))["EXTERNAL_SENDERS"]
ACTIONS = {
    1: ("copy_originals", "verify_originals"),
    2: ("start_monitoring", "start_core", "place_entry", "check_b17", "route_hq"),
    3: ("remove_old_entry", "split_memory"),
    4: ("retire_others", "retire_archive_to_hdd", "copy_old_todo", "create_old_todo_card",
        "collect_inventory", "resume_patrol"),
    5: ("check_public_browser",),
    6: ("enable_reap", "copy_cutover_log"),
}


class Backend(Protocol):
    def supports(self, action: str) -> bool: ...
    def check(self, name: str, ctx: Context) -> bool: ...
    def perform(self, action: str, ctx: Context, **kwargs): ...
    def todo(self, key: str, title: str, ctx: Context) -> None: ...


class PendingBackend:
    """No guessed CLI or implicit wiring to unfinished sibling packages."""
    def supports(self, action):
        return False

    def check(self, name, ctx):
        raise IntegrationPending("backend_not_connected")

    def perform(self, action, ctx, **kwargs):
        raise IntegrationPending("backend_not_connected")

    def todo(self, key, title, ctx):
        raise IntegrationPending("backend_not_connected")


@dataclass(frozen=True)
class Monitoring:
    observer_age_s: float
    observer_stale_s: float
    services: dict[str, tuple[str, int | None]]
    heartbeat_ages_s: tuple[float, ...]
    heartbeat_s: float
    stale_beats: int
    observed_s: float
    observe_s: float
    new_alerts: int
    manager_healthy: bool
    backup_healthy: bool

    def ok(self) -> bool:
        return (0 <= self.observer_age_s <= self.observer_stale_s
                and self.observer_stale_s > 0 and bool(self.services)
                and all(state == "running" or (name == "archive-init" and state == "exited" and code == 0)
                        for name, (state, code) in self.services.items())
                and bool(self.heartbeat_ages_s) and self.heartbeat_s > 0 and self.stale_beats > 0
                and all(0 <= age <= self.heartbeat_s * self.stale_beats for age in self.heartbeat_ages_s)
                and self.observe_s > 0 and self.observed_s >= 2 * self.observe_s
                and self.new_alerts == 0 and self.manager_healthy is True and self.backup_healthy is True)


def notification_accepted(http_status: int, notification_id: object) -> bool:
    return http_status == 200 and isinstance(notification_id, str) and bool(notification_id.strip())


def feature_states(credentials: dict[str, bool]) -> dict[str, dict]:
    # Feature credentials are not prerequisites. Reasons contain no credential values.
    return {name: {"enabled": verified is True, "reason": None if verified is True else "credential_unverified"}
            for name, verified in credentials.items()}


@dataclass
class StageResult:
    stage: int
    result: str
    actions: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    dry_run: bool = False


@dataclass
class Operator:
    ctx: Context
    backend: Backend = field(default_factory=PendingBackend)

    def _need(self, *names):
        missing = [name for name in names if self.backend.check(name, self.ctx) is not True]
        if missing:
            for name in missing:
                self.backend.todo("cutover/" + name, "切り替えの条件を確認する: " + name, self.ctx)
            raise GateBlocked("conditions_missing:" + ",".join(missing))

    def _do(self, action, *, changes=(), **kwargs):
        self.ctx.guard(changes)
        if self.backend.check("go_not_withdrawn", self.ctx) is not True:
            raise GateBlocked("go_withdrawn")
        return self.backend.perform(action, self.ctx, **kwargs)

    def _record(self, subject, result="ok", reason="", **kwargs):
        return self.ctx.note(subject, subject + "/" + self.ctx.profile.host, result, reason, **kwargs)

    def advance(self, stage: int) -> StageResult:
        if stage not in ACTIONS:
            raise ValueError("invalid_stage")
        if self.ctx.dry_run:
            return StageResult(stage, "planned", list(ACTIONS[stage]), dry_run=True)
        # Verify authority before backend loading can perform any operation.
        self.ctx.guard()
        missing = [name for name in ACTIONS[stage] if not self.backend.supports(name)]
        if missing:
            raise IntegrationPending("adapters_missing:" + ",".join(missing))
        self._need("go_not_withdrawn", "b0_b16_built")
        method = getattr(self, "_stage" + str(stage))
        return method()

    def _stage1(self):
        self._do("copy_originals", changes=(7,))
        self._do("verify_originals")
        self._record("orig-copy")
        return StageResult(1, "ok", list(ACTIONS[1]))

    def _stage2(self):
        self._need("originals_readable")
        self._do("start_monitoring")
        self._do("start_core")
        self._do("place_entry", changes=(7,))
        self._need("monitoring_normal", "ax_status_200", "entry_usable")
        results = self._do("check_b17")
        if not isinstance(results, dict):
            raise GateBlocked("b17_results_invalid")
        failed = [test for test in REAL_TESTS if test != "T-78" and results.get(test) != "ok"]
        if results.get("T-78") in ("ok", "failed"):
            self._record("T-78", results["T-78"])
        for test in failed:
            self.backend.todo("cutover/" + test, "実機試験を通す: " + test, self.ctx)
        if failed:
            raise GateBlocked("b17_not_passed:" + ",".join(failed))
        self._record("b17-checks")
        self._do("route_hq", changes=(6,))
        self._need("hq_route_8907", "tailnet_direct_200")
        self._record("entry-placed")
        return StageResult(2, "ok", list(ACTIONS[2]))

    def _stage3(self):
        self._need("originals_readable", "entry_usable", "ax_status_200", "monitoring_normal",
                   "hq_route_8907", "tailnet_direct_200", "b17_passed_except_t78")
        self._do("remove_old_entry", changes=(7,))
        self._do("split_memory", changes=(7,))
        self._record("old-entry-removed")
        return StageResult(3, "ok", list(ACTIONS[3]))

    def _stage4(self):
        # Do not require condition 1 here: it is a result of retirement, not a prerequisite.
        self._need("entry_usable", "old_entry_removed", "ax_status_200", "hq_route_8907",
                   "tailnet_direct_200", "b17_passed_except_t78", "running_jobs_zero",
                   "prerequisites_registered", "monitoring_normal", "external_collect_only")
        # Remote change 12 is checked per host by the retirement adapter. Its
        # absence must not prevent approved local retirement (F-15.2).
        self._do("retire_others", changes=(7,))
        records = self.ctx.records()
        t78_ok = latest_result(records, "T-78") == "ok"
        if t78_ok:
            self._need("archive_service_inactive", "archive_lock_free")
            self._do("retire_archive_to_hdd", changes=(7,))
        # Old ToDo is copied after its writer stops; never imported.
        self._need("old_todo_writer_stopped")
        self._do("copy_old_todo")
        self._do("create_old_todo_card", title="旧 ToDo の扱いを決める", idempotency_key="cutover/old-todo")
        if not t78_ok:
            self.backend.todo("cutover/T-78", "T-78 が通っていないため旧 archive-to-hdd が動いている", self.ctx)
            self._record("patrol", "waiting", "t78")
            return StageResult(4, "waiting", ["retire_others", "copy_old_todo", "create_old_todo_card"], ["T-78"])
        self._need("old_components_stopped", "archive_to_hdd_stopped", "running_jobs_zero",
                   "prerequisites_registered", "monitoring_normal", "external_collect_only")
        at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        event_id = self._record("cutover-time", at=at)
        self._do("collect_inventory", mode="collect_only", cutover_event_id=event_id)
        self._need("inventory_recorded", "cutover_time_visible", "inventory_mutations_zero",
                   "old_components_stopped", "running_jobs_zero", "prerequisites_registered", "monitoring_normal")
        self._do("resume_patrol", keep_collect_only=EXTERNAL_SENDERS)
        self._record("patrol")
        return StageResult(4, "ok", list(ACTIONS[4]))

    def _stage5(self):
        self._need("hq_route_8907", "tailnet_direct_200")
        result = self._do("check_public_browser")
        if result is not True:
            self.backend.todo("cutover/hq-access-email", "公開入口を確認する", self.ctx)
            self._record("public-browser", "waiting", "public_entry")
            return StageResult(5, "waiting", list(ACTIONS[5]), ["public_entry"])
        self._record("public-browser")
        return StageResult(5, "ok", list(ACTIONS[5]))

    def _stage6(self):
        self._need("archive_to_hdd_stopped", "old_components_stopped")
        if latest_result(self.ctx.records(), "T-78") != "ok":
            raise GateBlocked("t78")
        self._do("enable_reap")
        self._record("reap-enabled")
        self._do("copy_cutover_log")
        return StageResult(6, "ok", list(ACTIONS[6]))

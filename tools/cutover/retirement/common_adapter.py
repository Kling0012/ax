"""Adapter for b17-1 common.Context and its stage-4 backend actions."""
from __future__ import annotations

from dataclasses import dataclass
import shlex
from typing import Callable, Mapping, Sequence

from tools.cutover import common
from .engine import CommandResult, Context, Executor, Profile, StepFailure


def common_executor(ctx: common.Context, profile: Profile,
                    routes: Mapping[str, Sequence[str]]) -> Callable:
    """routes[host] is () locally, or an explicitly supplied SSH argv prefix.

    common.Runner has no stdin parameter. For the fixed filesystem helper only,
    wrap its JSON stdin in a Python literal. SSH receives one shlex-quoted argv
    string, so neither profile paths nor payloads undergo shell substitution.
    No connection is made until the returned callable is invoked.
    """
    def execute(host: str, argv: Sequence[str], stdin: str | None = None) -> CommandResult:
        if host not in routes:
            raise StepFailure("host-route-missing")
        ctx.guard((7,) if host == profile.local.name else (12,))
        cmd = list(argv)
        if stdin is not None:
            if len(cmd) < 4 or cmd[-3:-1] != ["-I", "-c"] or cmd[-4] != "python3":
                raise StepFailure("stdin-command-unsupported")
            program = cmd[-1]
            cmd[-1] = f"import io, sys; sys.stdin = io.StringIO({stdin!r}); exec(compile({program!r}, '<retirement-files>', 'exec'))"
        route = tuple(routes[host])
        result = ctx.run((*route, shlex.join(cmd)) if route else cmd,
                         timeout=60, changes=(7,) if host == profile.local.name else (12,))
        if result.planned:
            raise StepFailure("planned-command-not-executed")
        return CommandResult(result.returncode, result.stdout, result.stderr)
    return execute


def pending_noded_replacement(host) -> bool:
    """Only this proof adapter remains pending until B15/B17 checks the runner."""
    raise common.IntegrationPending("noded_t142_proof_adapter_required")


def from_common(ctx: common.Context, profile: Profile, *, execute: Callable,
                card: Callable[[str, str, str], None],
                verify_noded_replacement: Callable = pending_noded_replacement, **clock) -> Context:
    """Use common's verified recorder and live claim; no duplicate event writer."""
    def authorize():
        if ctx.dry_run:
            raise common.GateBlocked("retirement_requires_verified_apply")
        ctx.guard()

    def record(kind, subject, data):
        target = data["target"] if kind == "cutover.state" else data["component_id"]
        ctx.note(subject, target, data["result"], data.get("reason", ""), kind=kind)

    def latest(subject):
        entries = [e for e in ctx.records() if e.get("subject") == subject]
        return max(entries, key=lambda e: (e["ts"], e["id"])) if entries else None

    def guarded_execute(host, argv, stdin):
        # Retain change-scope verification even with a custom host transport.
        ctx.guard((7,) if host == profile.local.name else (12,))
        return execute(host, argv, stdin)

    return Context(guarded_execute, record, latest, card, authorize, verify_noded_replacement, **clock)


@dataclass
class RetirementSession:
    """Keep this instance between retire_others and retire_archive_to_hdd."""
    executor: Executor

    @classmethod
    def create(cls, context: Context, profile: Profile):
        return cls(Executor(context, profile))

    def retire_others(self):
        return self.executor.run(stop_archiver=False)

    def retire_archive_to_hdd(self):
        if not self.executor.r.steps:
            raise StepFailure("retire-others-first")
        self.executor.archive_last()
        result = self.executor.r
        result.condition1 = self.executor.verify_set()
        result.next_step = "check-condition3" if result.condition1 else "collect_only"
        return result

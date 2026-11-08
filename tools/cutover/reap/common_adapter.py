"""Adapter to b17-1 Context. Dry runs cannot reach filesystem mutation."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tools.cutover.common import Context, GateBlocked
from .operator import Paths, Ports, ReapOperator


def from_context(ctx: Context, *, sources_file: Path, changelog: Path,
                 restart_argv: Sequence[str]) -> ReapOperator:
    """Bind a B17 Context; compose argv comes from the reviewed deployment profile.

    Context.guard verifies approval and cutover ownership; Context.note obtains
    identity from the live claim, never from a supplied principal. Context.run
    remains the only path for systemd observations and archiver restart.
    """
    command = tuple(str(arg) for arg in restart_argv)
    if not command:
        raise ValueError("archiver_restart_command_required")

    def authorize():
        if ctx.dry_run:
            return False
        ctx.guard()
        return True

    def note(kind, subject, target, result, reason):
        identity = ctx.note(subject, target, result, reason, kind=kind)
        return 0 if identity is not None and not ctx.dry_run else 3

    def restart():
        response = ctx.run(command)
        return response.returncode == 0 and not response.planned

    paths = Paths(ctx.profile.ax_state_dir, sources_file, ctx.archive_dir,
                  changelog, ctx.profile.host)
    return ReapOperator(paths, Ports(authorize, ctx.run, note, restart))


class ReapBackend:
    """Delegate other B17 actions and implement stage 6's enable_reap action."""
    def __init__(self, delegate, operator: ReapOperator, *, t2_check):
        self.delegate, self.operator, self.t2_check = delegate, operator, t2_check

    def supports(self, action):
        return action == "enable_reap" or self.delegate.supports(action)

    def check(self, name, ctx):
        return self.delegate.check(name, ctx)

    def todo(self, key, title, ctx):
        return self.delegate.todo(key, title, ctx)

    def perform(self, action, ctx, **kwargs):
        if action != "enable_reap":
            return self.delegate.perform(action, ctx, **kwargs)
        result = self.operator.enable(t2_ok=self.t2_check() is True)
        if result.result == "ok" or (result.reason == "already_activated_use_resume"
                                      and self.operator.paths.marker.is_file()):
            return result
        raise GateBlocked(result.reason)

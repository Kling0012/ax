"""Explicit package wiring. Registration does not execute or import sibling tools.

Operations and fresh observations are supplied by the operator's reviewed local
adapter. The installed CLI deliberately has no guessed deployment commands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .common import Context, IntegrationPending


@dataclass
class BoundBackend:
    actions: dict[str, Callable] = field(default_factory=dict)
    checks: dict[str, Callable] = field(default_factory=dict)
    card: Callable | None = None

    def supports(self, action):
        return action in self.actions

    def check(self, name, ctx):
        if name not in self.checks:
            raise IntegrationPending("observation_not_connected:" + name)
        return self.checks[name](ctx)

    def perform(self, action, ctx, **kwargs):
        if action not in self.actions:
            raise IntegrationPending("action_not_connected:" + action)
        return self.actions[action](ctx, **kwargs)

    def todo(self, key, title, ctx):
        if self.card is None:
            raise IntegrationPending("todo_not_connected")
        ctx.guard()
        self.card(key, title, ctx)


def bind_files(backend: BoundBackend, *, home, generated_guide):
    """Use actual file operations without embedding a host home path."""
    from .files import copy_originals, copy_old_todo, place_entry, remove_old_entry

    backend.actions.update({
        "copy_originals": lambda ctx: copy_originals(ctx, home),
        "place_entry": lambda ctx: place_entry(ctx, home, generated_guide),
        "remove_old_entry": lambda ctx: remove_old_entry(ctx, home,
            new_entry_usable=backend.check("entry_usable", ctx)),
        "copy_old_todo": lambda ctx: copy_old_todo(ctx, home / ".claude/hub/state/todo.sqlite",
            writer_stopped=backend.check("old_todo_writer_stopped", ctx)),
    })
    return backend

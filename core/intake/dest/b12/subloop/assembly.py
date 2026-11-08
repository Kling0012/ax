"""Explicit dependency assembly for the subloop creation service."""
from __future__ import annotations

from pathlib import Path

from core.intake.dest.b12.common.backend import GitBackend
from core.intake.dest.b12b3.subloop import git_head_reader
from .bridge import CommonBridge, bind_events
from .service import Ports, Service
from .store import Store


def assemble(app, *, store_path, template_root, create_todo, branch_root,
             registered_count, registered, validate, approval, park, card, emit):
    """Install only when the trusted backend and all external ports are supplied.

    branch_root must return an immutable, separately isolated candidate view;
    a missing or unreadable remote must raise instead of looking like a missing
    branch. The caller supplies durable and authenticated adapters.
    """
    engine = getattr(app.state, "b12", None)
    backend = getattr(engine, "backend", None)
    if not isinstance(backend, GitBackend) or not backend.staging:
        raise ValueError("subloop trusted staging backend is not configured")
    ports = (create_todo, branch_root, registered_count, registered, validate,
             approval, park, card, emit)
    if not all(callable(port) for port in ports):
        raise ValueError("subloop service adapters are not configured")
    bridge = CommonBridge(app, validate=validate, template_root=Path(template_root))
    store = Store(store_path)
    try:
        service = Service(store, Ports(create_todo=create_todo,
            submit_item=bridge.submit_item, accept_branch=bridge.accept_branch,
            branch_root=branch_root, registered_count=registered_count,
            registered=registered, validate=validate, approval=approval, park=park,
            card=card, emit=emit, read_head=git_head_reader(backend.staging)),
            template_root=Path(template_root), stage=bridge.plan_registration)
        bind_events(app, service)
    except BaseException:
        store.close()
        raise
    app.state.b12_subloop = service
    return service

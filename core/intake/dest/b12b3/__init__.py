"""B12b3 intake destinations. Each handler module fails locally (fail-soft)."""
from __future__ import annotations

from core.intake.dest.b12b3 import change, claim, decision, ledger_updates, seat, secret, subloop, todo_ops, work_ops


def register(app):
    for name, register_one in (
        ("change", change.register),
        ("claim", claim.register),
        ("ledger_updates", ledger_updates.register),
        ("todo", todo_ops.register),
        ("work_ops", work_ops.register),
        ("seat", seat.register),
        ("decision", lambda app: app.state.intake.dest_handlers.__setitem__("decision.record", decision.handle)),
        ("secret", lambda app: app.state.intake.dest_handlers.__setitem__("secret.push", secret.handle)),
        ("subloop", lambda app: app.state.intake.dest_handlers.__setitem__("subloop.create", subloop.handle)),
    ):
        try:
            register_one(app)
        except Exception:  # noqa: BLE001
            fails = getattr(app.state, "ext_failures", None)
            if isinstance(fails, list):
                fails.append("b12b3:" + name)

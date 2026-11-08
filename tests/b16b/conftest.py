from __future__ import annotations

from types import SimpleNamespace

import pytest

from axcommon import ids, timeutil
from core.control.service import ControlService
from core.control.store import Store
from core.intake.dest.b16b.common import TargetRegistry, install_control
from core.intake.dest.b16b.delivery import DeliveryJournal
from tests.fakes.b16b.common import FakeEvents, FakePermissionReader, FakeSender
from tests.fakes.http import FakeTransport


@pytest.fixture(params=[("x-posts", "投稿"), ("discord-forward", "送信"), ("scheduled-publisher", "publish")])
def world(request, tmp_path):
    plugin_id, operation = request.param
    target = {"plugin_id": plugin_id, "project": "fictional", "repo": "fictional-repo",
              "destination": "fictional-destination", "operations": [operation]}
    registry = TargetRegistry([target])
    events = FakeEvents()
    store = Store(tmp_path / "control.sqlite3")
    store.upsert_plugin({"id": plugin_id, "kind": "timer", "path": "fictional/manifest.yaml",
        "manifest": {"id": plugin_id, "mode": "collect_only", "capabilities": ["run", "collect_only", "draining"],
                     "outputs": [operation], "knobs": {}, "execution": {"endpoint": "http://fictional:8080"}},
        "registered_at": timeutil.now_ts()})
    transport = FakeTransport().add("POST", "/apply", json={"ok": True})
    control = install_control(ControlService(store, events, applier_transport=transport), lambda: registry)
    journal = DeliveryJournal(tmp_path / "delivery.sqlite3", registry, emit=events.emit, lost=events.lost)
    sender = FakeSender(plugin_id)
    reference = ids.new_ulid()
    permission = {"project": target["project"], "repo": target["repo"], "destination": target["destination"],
                  "operations": [operation], "source_instruction": reference, "conditions": {}}
    w = SimpleNamespace(plugin_id=plugin_id, operation=operation, target=target, registry=registry,
                        events=events, store=store, control=control, journal=journal, sender=sender,
                        permission=permission, reference=reference, transport=transport)
    w.send = lambda **kw: journal.deliver(**{
        "mode": control.effective(plugin_id)["mode"], "target": target, "operation": operation,
        "source_item_id": "ax-fictional-candidate", "registered": True,
        "expected_destination": target["destination"], "permission_reader": FakePermissionReader(control),
        "preflight": lambda: True, "send": sender, **kw})
    yield w
    journal.close()
    store.close()


def start(world, permissions=None, **overrides):
    body = {"target": world.plugin_id, "op": "set_mode", "args": {"mode": "run", "permission_ids": permissions or []},
            "reason": "fictional user start", "corr": world.reference, **overrides}
    return world.control.issue(body, issued_by="user")

"""T-15 fixtures. All transports stay in memory; host requests stay in tmp_path."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from axcommon.client import ApiClient, LedgerClient, TimelineClient
from axcommon.events import EventSink
from core.intake.dest.b10a.runner import EmergencyConfig, EmergencyRunner
from tests.fakes.b10a.world import World


@pytest.fixture
def emergency(tmp_path, fake_clock):
    world = World()
    cfg = EmergencyConfig(host="test-host", data_dir=str(tmp_path / "data"), state_dir=str(tmp_path / "state"))
    timeline = TimelineClient("http://timeline:8080", transport=world)
    sink = EventSink(timeline, "emergency", cfg.host, tmp_path / "outbox")
    runner = EmergencyRunner(cfg, timeline=timeline,
                             control=ApiClient("http://control:8080", token="test-emergency", transport=world),
                             ledger=LedgerClient("http://ledger:8080", transport=world),
                             pool=ApiClient("http://worker-pool:8080", transport=world), sink=sink)
    world.control.add_plugin("alpha")
    world.control.add_plugin("beta", capabilities=("stopped", "draining"))
    registered = Path(cfg.state_dir) / "hostwatch" / "registered"
    registered.mkdir(parents=True)
    for name in ("manager", "inspector", "timer-runner", "sl-alpha", "sl-beta", "hq", "intake", "timeline", "observer", "unrelated"):
        (registered / name).touch()
    return SimpleNamespace(world=world, runner=runner, cfg=cfg, sink=sink, clock=fake_clock)

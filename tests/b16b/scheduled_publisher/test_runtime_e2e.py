import asyncio

import pytest
from starlette.testclient import TestClient

from axcommon.redact import Redactor
from core.intake.dest.b16b.common import TargetRegistry
from plugins.timer.scheduled_publisher import ScheduledPublisher
from plugins.timer.scheduled_publisher.integration import bind_runtime
from runtime.subloop.app import build_app
from runtime.subloop.graph import load_yaml
from tests.fakes.b7 import make_env
from tests.fakes.b16b.scheduled_publisher.parts import FakeCLI, FakeControl, FakeEnvironment, FakeWorker


class Sender:
    def __init__(self):
        self.calls = []
        self.lookups = []
        self.crash = False

    def submit(self, item, idempotency_key):
        self.calls.append(idempotency_key)
        if self.crash:
            raise ConnectionError("lost")
        return "receipt"

    def lookup(self, idempotency_key):
        self.lookups.append(idempotency_key)
        return "unknown"


@pytest.fixture
def world(tmp_path, repo_root, fake_clock):
    env = make_env(tmp_path, repo_root, fake_clock)
    env.svc.plugin_id = "scheduled-publisher"
    directory = repo_root / "plugins/timer/scheduled_publisher"
    env.svc.plugin_dir = directory
    ex = env.executor(load_yaml(directory / "graph.yaml"), load_yaml(directory / "manifest.yaml"))
    target = {"plugin_id": "scheduled-publisher", "project": "fixture", "repo": "fixture-repo",
              "destination": "fixture-account", "operations": ["publish"]}
    targets = TargetRegistry([target])
    control = FakeControl()
    cli = FakeCLI()
    box = FakeEnvironment("fixture", cli)
    worker = FakeWorker(box)
    lost = []
    engine = ScheduledPublisher(tmp_path / "publisher", registry=targets, control=control, environment=box,
                                worker=worker, redactor=Redactor(pii_list=[], allow_email_domains=[]),
                                facts="Fixture facts", cli=cli, public_gate=lambda _: True, lost=lost.append)
    engine.state.register_owner("fixture", user_confirmed=True)
    sender = Sender()
    commands, permissions = [], []
    def get(path, **kwargs):
        return {"commands": commands} if path == "/v1/commands" else {"permissions": permissions}
    env.svc.control.get = get
    bind_runtime(ex, engine=engine, sender=sender)
    app = build_app(ex, apply_token="token", run_loops=False)
    client = TestClient(app)

    def start_permission():
        control.mode = "run"
        control.allowed = True
        permissions.append({"id": "permission", "enabled": True, "source_instruction": "instruction",
                            "conditions": {}, "project": "fixture", "repo": "fixture-repo",
                            "destination": "fixture-account", "operations": ["publish"]})
        commands.append({"target": "scheduled-publisher", "issued_by": "user", "state": "active",
                         "op": "set_mode", "corr": "instruction",
                         "args": {"mode": "run", "permission_ids": ["permission"]}})

    def run(mode):
        response = client.post("/apply", headers={"Authorization": "Bearer token"},
                               json={"mode": mode, "revision": 1, "run_now": True})
        assert response.status_code == 200
        async def step():
            await ex.tick()
            await ex.drain()
        asyncio.run(step())
        fake_clock.advance(3600)

    yield env, ex, engine, box, sender, lost, start_permission, run, commands
    engine.close()
    client.close()


def test_default_and_collect_only_never_call_box(world):
    env, ex, engine, box, sender, lost, start, run, commands = world
    assert ex.mode == "collect_only"
    assert sender.calls == [] and box.selects == []
    run("collect_only")
    assert sender.calls == [] and box.selects == [] and box.calls == []


def test_no_start_instruction_never_sends(world):
    env, ex, engine, box, sender, lost, start, run, commands = world
    run("run")
    assert sender.calls == [] and box.selects == []


def test_unavailable_sandbox_never_sends(world):
    env, ex, engine, box, sender, lost, start, run, commands = world
    start()
    box.available = False
    run("run")
    assert sender.calls == []
    assert env.ledger.created


def test_sent_is_not_repeated_after_restart(world):
    env, ex, engine, box, sender, lost, start, run, commands = world
    start()
    run("run")
    assert len(sender.calls) == 1
    from plugins.timer.scheduled_publisher.state import State
    engine.state.close()
    engine.state = State(engine.state.root, engine.registry, emit=engine._emit, lost=lost.append)
    directory = env.root / "plugins/timer/scheduled_publisher"
    restarted = env.executor(load_yaml(directory / "graph.yaml"), load_yaml(directory / "manifest.yaml"))
    assert restarted.mode == "collect_only"
    bind_runtime(restarted, engine=engine, sender=sender)
    client = TestClient(build_app(restarted, apply_token="token", run_loops=False))
    response = client.post("/apply", headers={"Authorization": "Bearer token"},
                           json={"mode": "run", "revision": 2, "run_now": True})
    assert response.status_code == 200
    async def step():
        await restarted.tick()
        await restarted.drain()
    asyncio.run(step())
    client.close()
    assert len(sender.calls) == 1


def test_unknown_after_send_is_lost_and_not_retried(world):
    env, ex, engine, box, sender, lost, start, run, commands = world
    start()
    sender.crash = True
    run("run")
    assert len(sender.calls) == 1 and lost
    run("run")
    assert len(sender.calls) == 1 and set(sender.lookups) == set(sender.calls)
    assert engine.state.history()[0]["state"] == "unknown"


def test_unbound_sender_creates_one_todo(world):
    env, ex, engine, box, sender, lost, start, run, commands = world
    bind_runtime(ex)
    run("run")
    assert sender.calls == [] and len(env.ledger.created) == 1


def test_start_requires_permission_ids(world):
    env, ex, engine, box, sender, lost, start, run, commands = world
    start()
    commands[0]["args"]["permission_ids"] = []
    run("run")
    assert sender.calls == [] and box.selects == []


def test_registered_default_makes_no_external_jobs(tmp_path, repo_root, fake_clock):
    fragment = load_yaml(repo_root / "plugins/registry.d/b16b-scheduled-publisher.yaml")
    registration = next(row for row in fragment["plugins"] if row["id"] == "scheduled-publisher")
    directory = (repo_root / registration["path"]).parent
    manifest = load_yaml(directory / "manifest.yaml")
    assert manifest["default_mode"] == "collect_only"
    env = make_env(tmp_path, repo_root, fake_clock)
    env.svc.plugin_id = registration["id"]
    env.svc.plugin_dir = directory
    ex = env.executor(load_yaml(directory / manifest["graph"]), manifest)
    bind_runtime(ex)
    client = TestClient(build_app(ex, apply_token="token", run_loops=False))
    assert client.get("/status").json()["mode"] == "collect_only"
    response = client.post("/apply", headers={"Authorization": "Bearer token"},
                           json={"mode": "collect_only", "revision": 1, "run_now": True})
    assert response.status_code == 200
    async def step():
        await ex.tick()
        await ex.drain()
    asyncio.run(step())
    assert env.resources.posted == [] and env.pool.submitted == []
    client.close()

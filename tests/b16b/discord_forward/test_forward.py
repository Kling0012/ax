"""T-66, T-67, T-153, T-155: Discord-specific offline acceptance."""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from axcommon import schemas, timeutil
from core.intake.dest.b16b.common import PermissionReader, TargetRegistry
from tests.fakes.b16b.discord_forward.parts import Clock, Control, EnvRunner, Source, WebhookTransport

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "ax_discord_forward", ROOT / "plugins/timer/discord-forward/__init__.py",
    submodule_search_locations=[str(ROOT / "plugins/timer/discord-forward")])
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


@pytest.fixture
def world(tmp_path):
    clock, source, control, transport = Clock(), Source(), Control(), WebhookTransport()
    target = module.Target("demo", "demo", "sample_user", "discord-webhook-demo", "12345", "demo", "repo")
    registry = TargetRegistry([target.delivery_target()])
    events, lost, instances = [], [], []

    def build(**kwargs):
        engine = module.DiscordForwarder(
            tmp_path / "ax", [target], registry, PermissionReader(control), source,
            module.DiscordWebhook(transport=transport,
                                 secrets=lambda _: "https://discord.com/api/webhooks/1/fixture"),
            secret_scan=lambda content: "private" not in content,
            emit=lambda *args: events.append(args), lost=lost.append, clock=clock,
            start_delay_s=0, **kwargs)
        instances.append(engine)
        return engine

    world = SimpleNamespace(clock=clock, source=source, control=control, transport=transport,
                            target=target, registry=registry, events=events, lost=lost,
                            build=build, path=tmp_path)
    yield world
    for engine in instances:
        if engine.db is not None:
            engine.close()


def post(world, ident="1", *, text="hello", offset=1, **flags):
    return module.Post(ident, timeutil.format_ts(world.clock.value + timedelta(seconds=offset)),
                       text, **flags)


def close(engine):
    engine.close()
    engine.db = None


def test_t66_collect_only_and_two_step_boundary(world):
    engine = world.build()
    world.source.posts = [post(world)]
    result = engine.tick()
    assert result[0]["collect"]["count"] == 1
    assert engine.targets_view() == [world.target.delivery_target()]
    assert not world.transport.calls
    assert engine.cursor(world.target) is None
    assert engine.journal.get("discord-forward", "12345", "1") is None
    # The granted permission alone does not change mode.
    assert world.control.enabled
    assert engine.send_pending(world.target) == []
    assert engine.send_pending(world.target, mode="run")[0]["state"] == "sent"
    engine.send_pending(world.target, mode="run")
    assert len(world.transport.posts) == 1
    assert engine.cursor(world.target) == "1"


def test_t66_first_boot_boundary_and_filtering(world):
    engine = world.build()
    world.source.posts = [post(world, "1", offset=-1), post(world, "2", offset=0),
                          post(world, "3"), post(world, "4", reply=True),
                          post(world, "5", retweet=True)]
    result = engine.tick(mode="run")
    assert result[0]["collect"]["count"] == 1
    assert len(world.transport.posts) == 1
    assert engine.cursor(world.target) == "3"


def test_t66_channel_mismatch_creates_no_delivery_key(world):
    engine = world.build()
    world.source.posts = [post(world)]
    world.transport.channel_id = "54321"
    result = engine.tick(mode="run")
    assert result[0]["deliveries"][0]["state"] == "denied"
    assert not world.transport.posts
    assert engine.journal.get("discord-forward", "12345", "1") is None


def test_t153_revocation_and_bad_scope(world):
    engine = world.build()
    world.source.posts = [post(world)]
    engine.collect(world.target)
    world.control.enabled = False
    assert engine.send_pending(world.target, mode="run")[0]["state"] == "denied"
    assert world.control.events[-1]["result"] == "denied"
    assert engine.journal.get("discord-forward", "12345", "1") is None
    bad = replace(world.target, channel_id="54321")
    assert engine.send_pending(bad, mode="run")[0]["state"] == "denied"
    assert engine.collect(bad)["state"] == "denied"
    assert not world.transport.posts


def test_t153_revocation_during_get(world):
    engine = world.build()
    world.source.posts = [post(world)]
    world.transport.after_get = lambda: setattr(world.control, "enabled", False)
    engine.tick(mode="run")
    assert world.control.events[-1]["result"] == "denied"
    assert not world.transport.posts
    assert engine.journal.get("discord-forward", "12345", "1") is None


def test_t66_confirmed_failure_retried_across_restart(world):
    engine = world.build()
    world.source.posts = [post(world)]
    world.transport.outcomes = [429, "ok"]
    assert engine.tick(mode="run")[0]["deliveries"][0]["state"] == "failed"
    assert engine.cursor(world.target) is None
    close(engine)
    world.clock.advance(60)
    restarted = world.build()
    assert restarted.tick(mode="run")[0]["deliveries"][0]["state"] == "sent"
    assert len(world.transport.posts) == 2
    assert restarted.cursor(world.target) == "1"


@pytest.mark.parametrize("outcome", [TimeoutError("network"), 500, 408, "no_id"])
def test_t67_t153_unknown_is_lost_without_retry(world, outcome):
    engine = world.build()
    world.source.posts = [post(world)]
    world.transport.outcomes = [outcome]
    assert engine.tick(mode="run")[0]["deliveries"][0]["state"] == "unknown"
    assert world.lost[-1]["status"] == "lost"
    assert world.lost[-1]["state"] == "unknown"
    assert engine.cursor(world.target) is None
    engine.send_pending(world.target, mode="run")
    close(engine)
    world.clock.advance(60)
    restarted = world.build()
    restarted.tick(mode="run")
    assert len(world.transport.posts) == 1
    assert restarted.journal.get("discord-forward", "12345", "1")["state"] == "unknown"


def test_t67_launching_persisted_before_io_and_recovered(world):
    class Crash(BaseException):
        pass

    engine = world.build()
    world.source.posts = [post(world)]
    engine.collect(world.target)

    def crash():
        record = engine.journal.get("discord-forward", "12345", "1")
        assert record["state"] == "launching"
        raise Crash()

    world.transport.before_post = crash
    with pytest.raises(Crash):
        engine.send_pending(world.target, mode="run")
    close(engine)
    world.transport.before_post = None
    world.clock.advance(60)
    restarted = world.build()
    assert restarted.journal.get("discord-forward", "12345", "1")["state"] == "unknown"
    restarted.tick(mode="run")
    assert len(world.transport.posts) == 1
    assert world.lost[-1]["reason"] == "restart_during_send"


def test_t155_restart_skips_stopped_and_collected_backlog(world, monkeypatch):
    engine = world.build()
    world.source.posts = [post(world, "1")]
    engine.tick()  # Collected without any send attempt.
    close(engine)
    world.clock.advance(60)
    # The old cursor is deliberately larger than the new AX post ID.
    old = world.path / "legacy/.local/state/x2discord/demo.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"last_id": "999999"}))
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert "legacy" not in path.parts, "old cursor must never be read"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    restarted = world.build()
    world.source.posts += [post(world, "2", offset=-10), post(world, "3", offset=0), post(world, "4")]
    restarted.tick(mode="run")
    assert len(world.transport.posts) == 1
    assert restarted.cursor(world.target) == "4"
    close(restarted)
    world.clock.advance(60)
    again = world.build()
    again.tick(mode="run")
    assert len(world.transport.posts) == 1


def test_t66_secret_scan_and_mentions(world):
    engine = world.build()
    world.source.posts = [post(world, "1", text="private"), post(world, "2", text="@everyone hello")]
    engine.tick(mode="run")
    assert len(world.transport.posts) == 1
    payload = json.loads(world.transport.posts[0][2])
    assert payload["allowed_mentions"] == {"parse": []}
    assert engine.journal.get("discord-forward", "12345", "1") is None
    for kind, _, data in world.events:
        if kind == "automation.delivery":
            schemas.validate("ext/b16b/automation-delivery", data)
            assert "text" not in data


def test_t66_duplicate_claim_is_atomic(world):
    engine = world.build()
    world.source.posts = [post(world)]
    engine.collect(world.target)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _: engine.send_pending(world.target, mode="run"), range(2)))
    assert len(world.transport.posts) == 1


def test_source_uses_existing_entry_and_no_x_queue(world):
    expected = [post(world)]
    runner = EnvRunner(b"verified-runtime-fixture-required")
    source = module.UserPostsSource(runner, decoder=lambda artifact: expected)
    assert source.fetch(world.target) == expected
    request, artifacts = runner.calls[0]
    assert request["entry"] == {"kind": "entrypoint", "name": "x-user-posts", "args": {
        "user": "sample_user", "limit": 20, "retweets": False}}
    assert request["requires"] == ["x-session"]
    assert request["outputs"] == ["posts.json"]
    assert request["secrets"] == [] and not artifacts
    assert not request["irreversible"]
    assert request["job_id"].startswith("ax-discord-")
    with pytest.raises(NotImplementedError):
        module.UserPostsSource(runner).fetch(world.target)


def test_schedule_missed_poll_and_local_source_failure(world):
    engine = world.build()
    engine.next_poll = world.clock.value.timestamp() + 120
    assert engine.tick(mode="run") == []
    assert not world.source.calls
    world.clock.advance(120)
    world.source.fail = True
    assert engine.tick(mode="run")[0]["collect"]["state"] == "failed"
    world.clock.advance(1000)
    world.source.fail = False
    world.source.posts = [post(world)]
    engine.tick(mode="run")
    assert len(world.source.calls) == 2  # No catch-up polls.
    assert len(world.transport.posts) == 1


def test_t153_unlisted_permission_rejected(world):
    from core.intake.dest.b16b.common import ScopeError
    permission = {"id": "permission-demo", "project": "demo", "repo": "repo",
                  "destination": "54321", "operations": ["送信"], "conditions": {},
                  "source_instruction": "start-demo", "enabled": True}
    with pytest.raises((ScopeError, ValueError)):
        world.registry.validate_permission(permission, plugin_id="discord-forward")


def test_t66_webhook_rotation_cannot_change_verified_destination(world):
    transport = world.transport
    values = iter(["https://discord.com/api/webhooks/1/verified",
                   "https://discord.com/api/webhooks/2/changed"])
    webhook = module.DiscordWebhook(transport=transport, secrets=lambda _: next(values))
    assert webhook.channel_matches(world.target)
    assert webhook.send(world.target, "hello") == "98765"
    assert transport.posts[0][1] == "https://discord.com/api/webhooks/1/verified?wait=true"
    from core.intake.dest.b16b.delivery import NotSent
    with pytest.raises(NotSent):
        webhook.send(world.target, "hello")  # Verification is single-use.


@pytest.mark.parametrize("value", ["https://example.invalid/api/webhooks/1/fixture",
                                  "https://discord.com/api/webhooks/1/fixture?wait=false"])
def test_t153_invalid_webhook_never_receives_request(world, value):
    engine = world.build()
    engine.webhook.secrets = lambda _: value
    world.source.posts = [post(world)]
    engine.tick(mode="run")
    assert not world.transport.calls
    assert engine.journal.get("discord-forward", "12345", "1") is None


def test_t66_target_config_uses_registered_scope(world):
    path = world.path / "discord-targets.yaml"
    record = {key: value for key, value in world.target.__dict__.items() if key not in ("project", "repo")}
    path.write_text(json.dumps({"targets": [record]}))
    assert module.load_targets(path, world.registry) == [world.target]
    record["channel_id"] = "54321"
    path.write_text(json.dumps([record]))
    with pytest.raises(ValueError, match="scope_not_unique"):
        module.load_targets(path, world.registry)

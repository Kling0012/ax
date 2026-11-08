from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from axcommon import ids, schemas, timeutil
from axcommon.creds import TokenTable, sha256_hex
from axcommon.hq_proof import encode
from core.hq.approval.security import Identity, VerifiedAction
from core.control.app import create_core_app
from core.intake.app import build_app
from core.intake.dest.b16b import register
from core.intake.dest.b16b.common import PermissionReader
from core.intake.vias import Ident
from core.intake.seat.store import key_fingerprint
from tests.fakes.http import FakeTransport


@pytest.fixture
def mouths(world, tmp_path, repo_root, monkeypatch):
    monkeypatch.setenv("AX_STATE_DIR", str(tmp_path / "state"))
    key = tmp_path / "hq-proof-key"
    key.write_bytes(b"fictional-test-key-01234567890123456789")
    monkeypatch.setenv("AX_HQ_PROOF_KEY_FILE", str(key))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    token = "fictional-b16b-intake-token"
    (secrets / "core-token-intake").write_text(token)
    monkeypatch.setenv("AX_SECRETS_PATH", str(secrets))
    control_app = create_core_app(world.control)
    control_app.state.core_tokens = TokenTable({"intake": sha256_hex(token)})
    control_client = TestClient(control_app)
    transport = FakeTransport()

    def relay(call):
        response = control_client.request(call.method, call.path, headers=call.headers, json=call.json)
        return response.status_code, dict(response.headers), response.content

    for method, path in [("GET", "/v1/plugins"), ("POST", "/v1/permissions"),
                         ("POST", "/v1/commands"), ("POST", "/v1/permissions/check")]:
        transport.add(method, path, handler=relay)
    config = tmp_path / "config"
    (config / "intake-destinations.d").mkdir(parents=True)
    (config / "intake-destinations.d" / "b16b.yaml").write_text(
        (repo_root / "config/intake-destinations.d/b16b.yaml").read_text())
    app = build_app(config_dir=config, dest_dir=tmp_path / "no-packages",
                    transport=transport, sink=world.events, run_heartbeat_loop=False)
    register(app)
    store = app.state.seats.store
    store.initialize()
    algorithm = b"ssh-ed25519"
    blob = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", 32) + b"x" * 32
    public_key = "ssh-ed25519 " + base64.b64encode(blob).decode()
    now = timeutil.now_ts()
    store.put({"seat_id": "fictional-seat", "label": "test", "via": "ssh",
               "device": "test-device", "device_id": "test-device-id", "tier": "irreversible",
               "state": "active", "created_at": now, "created_by": "test",
               "expires_at": timeutil.add_seconds(now, 86400), "public_key": public_key,
               "key_fp": key_fingerprint(public_key)})
    app.state.approvals.policy.manifest_reader = lambda plugin_id: world.store.get_plugin(plugin_id)["manifest"]
    ident = [Ident("hq", "hq", "user", "irreversible")]

    async def mouth(scope, receive, send):
        scope["ax_ident"] = ident[0]
        await app(scope, receive, send)

    client = TestClient(mouth)
    yield client, ident, transport
    client.close()
    control_client.close()
    app.state.intake.executor.shutdown()


def instruction(destination, payload, **extras):
    return {"id": ids.new_ulid(), "destination": destination, "payload": payload,
            "idempotency_key": ids.new_ulid(), "confirm_irreversible": True, **extras}


def signed_instruction(client, body):
    path = "/v1/instructions"
    unsigned = client.post(path, json=body)
    assert unsigned.status_code == 403, unsigned.text
    _, headers = encode("POST", path, body, VerifiedAction(Identity("test-user", "test-session", True), True))
    return client.post(path, json=body, headers=headers)


def test_t152_real_intake_two_steps(world, mouths):
    client, ident, transport = mouths
    response = signed_instruction(client, instruction("permission.change", world.permission))
    assert response.status_code == 201, response.text
    permission_id = response.json()["result"]["id"]
    assert world.control.plugin_view(world.plugin_id)["mode"] == "collect_only"
    assert not world.sender.calls
    body = {"target": world.plugin_id, "op": "set_mode", "args": {"mode": "run", "permission_ids": [permission_id]},
            "reason": "fictional user start", "corr": world.reference}
    response = client.post("/v1/instructions", json=instruction("control.command", body))
    assert response.status_code == 200, response.text
    command = response.json()["result"]
    assert command["issued_by"] == "user" and command["expires_at"] is None
    assert world.send()["state"] == "sent"
    reader = PermissionReader(__import__("axcommon.client", fromlist=["ApiClient"]).ApiClient(
        "http://control:8080", transport=transport))
    assert reader.check(world.target, world.operation)
    world.control.disable_permission(permission_id, by="user")
    assert not reader.check(world.target, world.operation)


def test_t152_commander_steps_wait_for_approval(world, mouths):
    client, ident, transport = mouths
    ident[0] = Ident("ssh", "commander", "", "read")
    client.headers["x-ax-seat"] = "fictional-seat"
    response = client.post("/v1/instructions", json=instruction("permission.change", world.permission,
                                                                confirm_irreversible=False))
    assert response.status_code == 202, response.text
    assert not world.store.list_permissions()
    permission = world.control.create_permission(world.permission, by="user")
    body = {"target": world.plugin_id, "op": "set_mode", "args": {"mode": "run", "permission_ids": [permission["id"]]},
            "reason": "fictional user start", "corr": world.reference}
    response = client.post("/v1/instructions", json=instruction("control.command", body,
                                                                confirm_irreversible=False))
    assert response.status_code == 202, response.text
    assert world.control.effective(world.plugin_id)["mode"] == "collect_only"
    assert not world.sender.calls
    assert not any(call.path == "/v1/commands" for call in transport.calls)


@pytest.mark.parametrize("invalid", ["no_target", "unknown_target", "mixed_operations"])
def test_t153_reject_before_response(world, mouths, invalid):
    client, _, transport = mouths
    permission = dict(world.permission)
    if invalid == "no_target":
        permission.pop("destination")
    elif invalid == "unknown_target":
        permission["destination"] = "unlisted"
    else:
        permission["operations"] = [world.operation, "unlisted"]
    response = client.post("/v1/instructions", json=instruction("permission.change", permission))
    assert response.status_code == 422, response.text
    assert not world.store.list_permissions()
    assert not any(call.path == "/v1/permissions" for call in transport.calls)
    assert any(event["kind"] == "instruction.rejected" for event in world.events.events)


def test_t153_unconfirmed_hq_cannot_start(world, mouths):
    client, _, _ = mouths
    permission = world.control.create_permission(world.permission, by="user")
    body = {"target": world.plugin_id, "op": "set_mode", "args": {"mode": "run", "permission_ids": [permission["id"]]},
            "reason": "fictional start", "corr": world.reference}
    response = client.post("/v1/instructions", json=instruction("control.command", body,
                                                                confirm_irreversible=False))
    assert response.status_code == 202, response.text
    assert world.control.effective(world.plugin_id)["mode"] == "collect_only"


def test_b16b_schemas_and_destination_fragment(repo_root):
    import jsonschema
    import yaml

    for path in (repo_root / "schemas/ext/b16b").glob("*.json"):
        jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))
    schema = yaml.safe_load((repo_root / "config/intake-destinations.d/b16b.yaml").read_text())
    schemas.validate("intake-destinations", schema)
    assert {entry["name"] for entry in schema} == {"permission.change", "control.command"}


def test_t153_permission_change_can_disable(world, mouths):
    client, _, _ = mouths
    permission = world.control.create_permission(world.permission, by="user")
    payload = {**world.permission, "id": permission["id"], "enabled": False}
    response = signed_instruction(client, instruction("permission.change", payload))
    assert response.status_code == 201, response.text
    assert world.store.get_permission(permission["id"])["enabled"] is False


def test_other_permission_operations_keep_the_existing_route(world, mouths):
    client, _, _ = mouths
    payload = {**world.permission, "operations": ["merge"], "destination": "fictional-deploy"}
    response = signed_instruction(client, instruction("permission.change", payload))
    assert response.status_code == 201, response.text
    assert response.json()["result"]["operations"] == ["merge"]


def test_plugins_list_exposes_delivery_targets_and_registry_can_read_them(world):
    from core.control.app import create_core_app
    from core.intake.dest.b16b.common import TargetRegistry
    from starlette.testclient import TestClient

    client = TestClient(create_core_app(world.control))
    try:
        response = client.get("/v1/plugins")
        assert response.status_code == 200, response.text
        plugins = response.json()["plugins"]

        class ControlClient:
            @staticmethod
            def get(path):
                assert path == "/v1/plugins"
                return {"plugins": plugins}

        registry = TargetRegistry.from_client(ControlClient())
    finally:
        client.close()

    assert registry.for_plugin(world.plugin_id) == [world.target]


def test_t152_public_api_to_delivery_and_revocation(world, mouths):
    from axcommon.client import ApiClient

    client, _, transport = mouths
    reader = PermissionReader(ApiClient("http://control:8080", transport=transport))

    def send(item):
        return world.journal.deliver(
            mode=world.control.effective(world.plugin_id)["mode"], target=world.target,
            operation=world.operation, source_item_id=item, registered=True,
            expected_destination=world.target["destination"], permission_reader=reader,
            preflight=lambda: True, send=world.sender)

    assert send("before-permission")["state"] == "skipped"
    response = signed_instruction(client, instruction("permission.change", world.permission))
    assert response.status_code == 201, response.text
    permission_id = response.json()["result"]["id"]
    assert send("before-start")["state"] == "skipped"
    assert not world.sender.calls
    assert world.journal.get(world.plugin_id, world.target["destination"], "before-start") is None

    command = {"target": world.plugin_id, "op": "set_mode",
               "args": {"mode": "run", "permission_ids": [permission_id]},
               "reason": "fictional user start", "corr": world.reference}
    response = client.post("/v1/instructions", json=instruction("control.command", command))
    assert response.status_code == 200, response.text
    result = send("after-start")
    assert result["state"] == "sent"
    assert world.journal.get(world.plugin_id, world.target["destination"], "after-start")["state"] == "sent"
    assert len(world.sender.calls) == 1

    response = signed_instruction(client, instruction("permission.change", {
        **world.permission, "id": permission_id, "enabled": False}))
    assert response.status_code == 201, response.text
    assert send("after-revocation")["state"] == "denied"
    assert world.journal.get(world.plugin_id, world.target["destination"], "after-revocation") is None
    assert len(world.sender.calls) == 1


def test_t152_permission_check_exception_denies_without_record(world, mouths):
    client, _, _ = mouths
    response = signed_instruction(client, instruction("permission.change", world.permission))
    assert response.status_code == 201, response.text
    command = {"target": world.plugin_id, "op": "set_mode",
               "args": {"mode": "run", "permission_ids": [response.json()["result"]["id"]]},
               "reason": "fictional user start", "corr": world.reference}
    response = client.post("/v1/instructions", json=instruction("control.command", command))
    assert response.status_code == 200, response.text

    class Unavailable:
        def check(self, target, operation):
            raise OSError("fictional outage")

    result = world.journal.deliver(
        mode=world.control.effective(world.plugin_id)["mode"], target=world.target,
        operation=world.operation, source_item_id="outage", registered=True,
        expected_destination=world.target["destination"], permission_reader=Unavailable(),
        preflight=lambda: True, send=world.sender)
    assert result == {"state": "denied", "reason": "permission_unavailable"}
    assert world.journal.get(world.plugin_id, world.target["destination"], "outage") is None
    assert not world.sender.calls


def test_t152_discord_public_entry_to_fake_webhook(world, mouths, tmp_path, repo_root):
    if world.plugin_id != "discord-forward":
        pytest.skip("Discord route only")
    import importlib.util
    import sys
    from datetime import timedelta
    from axcommon.client import ApiClient
    from tests.fakes.b16b.discord_forward.parts import Clock, Source, WebhookTransport

    path = repo_root / "plugins/timer/discord-forward/__init__.py"
    spec = importlib.util.spec_from_file_location("ax_discord_sendgate", path,
                                                submodule_search_locations=[str(path.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    target = module.Target("demo", "demo", "sample_user", "discord-webhook-demo", "12345",
                           "fictional", "fictional-repo")
    world.registry.targets[:] = [target.delivery_target()]
    world.permission["destination"] = "12345"
    client, _, transport = mouths
    clock, source, webhook = Clock(), Source(), WebhookTransport()
    source.posts = [module.Post("1", timeutil.format_ts(clock.value + timedelta(seconds=1)), "fixture")]
    engine = module.DiscordForwarder(
        tmp_path / "discord", [target], world.registry,
        PermissionReader(ApiClient("http://control:8080", transport=transport)), source,
        module.DiscordWebhook(transport=webhook, secrets=lambda _: "https://discord.com/api/webhooks/1/fixture"),
        secret_scan=lambda _: True, emit=world.events.emit, lost=world.events.lost,
        clock=clock, start_delay_s=0)
    try:
        assert engine.collect(target)["count"] == 1
        assert not engine.send_pending(target, mode=world.control.effective(world.plugin_id)["mode"])
        response = signed_instruction(client, instruction("permission.change", world.permission))
        assert response.status_code == 201, response.text
        permission_id = response.json()["result"]["id"]
        assert not engine.send_pending(target, mode=world.control.effective(world.plugin_id)["mode"])
        assert not webhook.posts
        command = {"target": world.plugin_id, "op": "set_mode",
                   "args": {"mode": "run", "permission_ids": [permission_id]},
                   "reason": "fictional user start", "corr": world.reference}
        response = client.post("/v1/instructions", json=instruction("control.command", command))
        assert response.status_code == 200, response.text
        assert engine.send_pending(target, mode=world.control.effective(world.plugin_id)["mode"])[0]["state"] == "sent"
        assert engine.journal.get("discord-forward", "12345", "1")["state"] == "sent"
        assert len(webhook.posts) == 1
        response = signed_instruction(client, instruction("permission.change", {
            **world.permission, "id": permission_id, "enabled": False}))
        assert response.status_code == 201, response.text
        source.posts = [module.Post("2", timeutil.format_ts(clock.value + timedelta(seconds=2)), "fixture")]
        engine.collect(target)
        assert engine.send_pending(target, mode=world.control.effective(world.plugin_id)["mode"])[0]["state"] == "denied"
        assert engine.journal.get("discord-forward", "12345", "2") is None
        assert len(webhook.posts) == 1
    finally:
        engine.close()


@pytest.fixture
def delivery_control_client(world):
    """Control HTTP without sockets; permit-check faults are injected at transport."""
    from urllib.parse import urlsplit
    from axcommon.client import ControlClient

    app = create_core_app(world.control)
    client = TestClient(app)
    fault = {"permission_check": False}

    def transport(method, url, headers, body, timeout):
        parts = urlsplit(url)
        if parts.path == "/v1/permissions/check" and fault["permission_check"]:
            raise OSError("fictional permission-check outage")
        path = parts.path + ("?" + parts.query if parts.query else "")
        response = client.request(method, path, headers=headers, content=body)
        return response.status_code, dict(response.headers), response.content

    yield ControlClient("http://control:8080", transport=transport), fault
    client.close()


def start_via_intake(world, client):
    response = signed_instruction(client, instruction("permission.change", world.permission))
    assert response.status_code == 201, response.text
    permission_id = response.json()["result"]["id"]
    return permission_id


def command_via_intake(world, client, permission_id):
    command = {"target": world.plugin_id, "op": "set_mode",
               "args": {"mode": "run", "permission_ids": [permission_id]},
               "reason": "fictional user start", "corr": world.reference}
    response = client.post("/v1/instructions", json=instruction("control.command", command))
    assert response.status_code == 200, response.text


def revoke_via_intake(world, client, permission_id):
    response = signed_instruction(client, instruction("permission.change", {
        **world.permission, "id": permission_id, "enabled": False}))
    assert response.status_code == 201, response.text


def test_t152_x_public_entry_to_fake_destination(world, mouths, delivery_control_client,
                                                tmp_path, repo_root, fake_clock):
    if world.plugin_id != "x-posts":
        pytest.skip("X route only")
    import importlib.util
    import sys

    path = repo_root / "plugins/timer/x-posts/__init__.py"
    spec = importlib.util.spec_from_file_location("x_posts", path,
                                                submodule_search_locations=[str(path.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    from tests.fakes.b16b.x_posts.components import Environment, Events, Inspector, Worker

    client, _, _ = mouths
    http_control, fault = delivery_control_client
    adapter = module.ControlAdapter(http_control)
    target = module.Target("x-posts", "fictional", "fictional-repo", "fictional-destination", "demo_account")
    env, events, inspector, worker = Environment(), Events(), Inspector(), Worker()
    engine = module.XPosts(tmp_path / "x-state", control=adapter, environment=module.XEnvironment(env),
                           worker=worker, inspector=inspector, events=events, lost=lambda _: None)
    try:
        assert engine.send_one(target)["state"] == "skipped"
        permission_id = start_via_intake(world, client)
        assert engine.send_one(target)["state"] == "skipped"
        assert not env.posts
        command_via_intake(world, client, permission_id)
        worker.proposals = [module.Draft("first", "fictional first post", timeutil.now_ts())]
        assert engine.collect(target, "first", scheduled_at=timeutil.now_ts())["result"] == "drafted"
        first = engine.store.candidates(target)[0]["id"]
        fault["permission_check"] = True
        assert engine.send_one(target, candidate_id=first)["state"] == "denied"
        assert engine.journal.get("x-posts", target.destination, first) is None
        assert not env.posts
        fault["permission_check"] = False
        assert engine.send_one(target, candidate_id=first)["state"] == "sent"
        assert engine.journal.get("x-posts", target.destination, first)["state"] == "sent"
        assert len(env.posts) == 1
        fake_clock.advance(4 * 3600)
        worker.proposals = [module.Draft("second", "fictional second post", timeutil.now_ts())]
        assert engine.collect(target, "second", scheduled_at=timeutil.now_ts())["result"] == "drafted"
        second = next(row["id"] for row in engine.store.candidates(target) if row["id"] != first)
        revoke_via_intake(world, client, permission_id)
        assert engine.send_one(target, candidate_id=second)["state"] == "denied"
        assert engine.journal.get("x-posts", target.destination, second) is None
        assert len(env.posts) == 1
    finally:
        engine.close()


def test_t152_scheduled_public_entry_to_fake_destination(world, mouths, delivery_control_client,
                                                        tmp_path, fake_clock):
    if world.plugin_id != "scheduled-publisher":
        pytest.skip("scheduled publisher route only")
    from axcommon.redact import Redactor
    from core.intake.dest.b16b.common import PermissionReader
    from plugins.timer.scheduled_publisher import ScheduledPublisher
    from tests.fakes.b16b.scheduled_publisher.parts import FakeCLI, FakeEnvironment, FakeWorker

    client, _, _ = mouths
    http_control, fault = delivery_control_client
    class Control:
        effective = http_control.effective
        check = PermissionReader(http_control).check

    cli = FakeCLI()
    env = FakeEnvironment("fictional-provider", cli)
    worker = FakeWorker(env)
    engine = ScheduledPublisher(tmp_path / "scheduled", registry=world.registry, control=Control(),
        environment=env, worker=worker, redactor=Redactor(pii_list=[], allow_email_domains=[]),
        facts="Fictional project facts.", cli=cli, public_gate=lambda _: True,
        emit=world.events.emit, lost=world.events.lost)
    engine.state.register_owner(env.provider, user_confirmed=True)
    try:
        assert engine.run(world.target, scheduled_at=timeutil.now_ts())["result"] == "collected"
        permission_id = start_via_intake(world, client)
        assert engine.run(world.target, scheduled_at=timeutil.now_ts())["result"] == "collected"
        assert not cli.launched
        command_via_intake(world, client, permission_id)
        fault["permission_check"] = True
        result = engine.run(world.target, scheduled_at=timeutil.now_ts())
        assert result["result"] == "denied"
        assert not cli.launched and not engine.state.history()
        fault["permission_check"] = False
        result = engine.run(world.target, scheduled_at=timeutil.now_ts())
        assert result["result"] == "succeeded", result
        assert result["deliveries"][0]["state"] == "sent"
        assert engine.state.history()[0]["state"] == "sent"
        assert len(cli.launched) == 1
        revoke_via_intake(world, client, permission_id)
        fake_clock.advance(3600)
        result = engine.run(world.target, scheduled_at=timeutil.now_ts())
        assert result["result"] == "denied"
        assert len(cli.launched) == 1 and len(engine.state.history()) == 1
    finally:
        engine.close()


def test_discord_timer_entry_permissions_restart_and_delivery(world, mouths, tmp_path, repo_root, monkeypatch):
    if world.plugin_id != 'discord-forward':
        pytest.skip('Discord route only')
    import asyncio
    from datetime import timedelta
    from urllib.parse import urlsplit
    from types import SimpleNamespace
    import yaml
    import axcommon.client as client_module
    from core.control.app import create_work_app
    from runtime.subloop.store import Store as TimerStore
    from runtime.timer_runner.runner import TimerRunner, load_handler
    from tests.fakes.b16b.discord_forward.parts import Clock, Source, WebhookTransport

    mouth, _, _ = mouths
    target = {'plugin_id': 'discord-forward', 'project': 'fictional', 'repo': 'fictional-repo',
              'destination': '12345', 'operations': ['送信']}
    world.registry.targets[:] = [target]
    world.permission['destination'] = '12345'
    work = create_work_app(world.control)
    work.state.plugin_tokens = TokenTable({'discord-forward': sha256_hex('fixture-plugin-token')})
    work_client = TestClient(work)
    query = {'project': 'fictional', 'repo': 'fictional-repo', 'destination': '12345', 'operation': '送信'}
    assert work_client.get('/v1/permissions/check', params=query).status_code == 401
    assert work_client.post('/v1/permissions/check', json=query).status_code in (404, 405)
    secret_dir = Path(__import__('os').environ['AX_SECRETS_PATH'])
    (secret_dir / 'plugin-token-discord-forward').write_text('fixture-plugin-token')
    config = tmp_path / 'targets.yaml'
    config.write_text(yaml.safe_dump({'targets': [{'id': 'demo', 'label': 'demo', 'x_user': 'sample_user',
        'secret': 'discord-webhook-demo', 'channel_id': '12345', 'include_retweets': False,
        'include_replies': False}]}))
    index, pii = tmp_path / 'index', tmp_path / 'pii'
    index.write_text('fixture-salt\n'); pii.write_text('')
    environment = {'AX_DISCORD_TARGETS_FILE': str(config), 'AX_REDACT_INDEX': str(index), 'AX_PII_LIST': str(pii)}
    handler = load_handler(repo_root / 'plugins/timer/discord-forward')
    factory_type, collector_type = handler.__globals__['DiscordFactory'], handler.__globals__['DiscordCollector']
    clock, source, webhook = Clock(), Source(), WebhookTransport()
    module = factory_type._modules()
    fake_webhook = module.DiscordWebhook(transport=webhook, secrets=lambda _: 'https://discord.com/api/webhooks/1/fixture')
    offline = [False]
    def transport(method, url, headers, body, timeout):
        if offline[0]:
            raise OSError('fixture control offline')
        parsed = urlsplit(url)
        response = work_client.request(method, parsed.path + ('?' + parsed.query if parsed.query else ''),
                                       headers=headers, content=body)
        return response.status_code, dict(response.headers), response.content
    real_client = client_module.ApiClient
    monkeypatch.setattr(client_module, 'ApiClient', lambda base, token=None, **kw: real_client(base, token, transport=transport))
    class Sink:
        def emit(self, *args, **kwargs):
            pass
    todos = []
    class Ledger:
        def post(self, path, body):
            assert path == '/v1/todos'
            todos.append(body)
    def make_timer():
        factory = factory_type(tmp_path, environ=environment, source=source, webhook=fake_webhook, clock=clock)
        collector = collector_type(tmp_path, environ=environment)
        store = TimerStore(tmp_path / 'timer.sqlite')
        runner = TimerRunner('timer-runner', store, Sink(), {'discord-forward': handler},
            {'discord-forward': {'collect_only_handler': True}}, lambda *args: Ledger(),
            require_effective=True, extra_clients={'discord-forward': {'collect_only': collector, 'discord_forward': factory}})
        return runner, store, factory
    def run(runner):
        return asyncio.run(runner.run_job('discord-forward', timeutil.format_ts(clock())))
    timer, store, factory = make_timer()
    timer.set_effective('timer-runner', {'mode': 'run', 'revision': 1})
    timer.set_effective('discord-forward', {'mode': 'collect_only', 'revision': 1})
    world.registry.targets[:] = []
    environment['AX_DISCORD_TARGETS_FILE'] = str(tmp_path / 'absent-targets.yaml')
    run(timer)
    assert not webhook.posts
    world.registry.targets[:] = [target]
    environment['AX_DISCORD_TARGETS_FILE'] = str(config)
    run(timer)
    assert not webhook.posts
    response = signed_instruction(mouth, instruction('permission.change', world.permission))
    assert response.status_code == 201
    permission_id = response.json()['result']['id']
    run(timer)
    assert not webhook.posts
    command = {'target': 'discord-forward', 'op': 'set_mode',
        'args': {'mode': 'run', 'permission_ids': [permission_id]}, 'reason': 'fixture', 'corr': world.reference}
    assert mouth.post('/v1/instructions', json=instruction('control.command', command)).status_code == 200
    timer.set_effective('discord-forward', {'mode': 'run', 'revision': 2})
    clock.advance(121)
    source.posts = [module.Post('1', timeutil.format_ts(clock() - timedelta(seconds=122)), 'old'),
                    module.Post('2', timeutil.format_ts(clock() - timedelta(seconds=1)), 'new')]
    offline[0] = True
    run(timer)
    assert not webhook.posts
    offline[0] = False
    response = work_client.get('/v1/permissions/check', params=query, headers={'Authorization': 'Bearer fixture-plugin-token'})
    assert response.json() == {'allowed': True}
    assert work_client.get('/v1/permissions/check', params={**query, 'destination': '54321'}, headers={'Authorization': 'Bearer fixture-plugin-token'}).json() == {'allowed': False}
    run(timer)
    assert len(webhook.posts) == 1, (source.calls, webhook.calls, factory.engine.targets_view() if factory.engine else None, list(factory.engine.db.execute('select * from candidates')) if factory.engine else None, store.runs(), timer._running)
    clock.advance(301)
    offline[0] = True
    run(timer)
    assert len(webhook.posts) == 1
    offline[0] = False
    clock.advance(301)
    run(timer)
    assert len(webhook.posts) == 1
    webhook.channel_id = '54321'
    source.posts = [module.Post('3', timeutil.format_ts(clock() + timedelta(seconds=1)), 'mismatch')]
    clock.advance(301)
    run(timer)
    assert len(webhook.posts) == 1
    webhook.channel_id = '12345'; webhook.outcomes = [429, 'ok']
    clock.advance(301); run(timer)
    assert len(webhook.posts) == 2
    factory.engine.close(); store.close()
    source.posts.append(module.Post('5', timeutil.format_ts(clock() - timedelta(seconds=1)), 'old before restart'))
    timer, store, factory = make_timer()
    run(timer)
    assert len(webhook.posts) == 2
    timer.set_effective('timer-runner', {'mode': 'run', 'revision': 1})
    timer.set_effective('discord-forward', {'mode': 'run', 'revision': 2})
    clock.advance(121); run(timer)
    assert len(webhook.posts) == 3
    assert factory.engine.journal.get('discord-forward', '12345', '5') is None
    source.posts = [module.Post('4', timeutil.format_ts(clock() + timedelta(seconds=1)), 'unknown')]
    webhook.outcomes = [OSError('unknown')]
    clock.advance(301); run(timer)
    assert len(webhook.posts) == 4
    clock.advance(301); run(timer)
    assert len(webhook.posts) == 4
    assert len(todos) == 1 and todos[0]['title'] == 'Discord delivery requires reconciliation'
    response = signed_instruction(mouth, instruction('permission.change', {**world.permission, 'id': permission_id, 'enabled': False}))
    assert response.status_code == 201
    assert work_client.get('/v1/permissions/check', params=query, headers={'Authorization': 'Bearer fixture-plugin-token'}).json() == {'allowed': False}
    factory.engine.close(); store.close(); work_client.close()

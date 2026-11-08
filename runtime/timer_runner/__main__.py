"""timer-runner の起動。登録簿から kind=timer のプラグインを読み、定期ジョブとタイマーを動かす。"""
from __future__ import annotations

import asyncio
import logging
import json
import os
import sys
from pathlib import Path

log = logging.getLogger("ax.timer_runner")
MODES = ("run", "collect_only", "poll_paused", "draining")


class ConversationSource:
    def __init__(self, history):
        self.history = Path(history)

    def changes(self, cursor):
        import hashlib
        if not self.history.is_dir():
            raise ValueError("history_source_unavailable")
        root = self.history.resolve()
        for path in sorted(self.history.rglob("*.jsonl")):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("history_record_too_large")
            data = path.read_bytes()
            messages = []
            for line in data.splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if isinstance(entry, dict) and entry.get("type") in ("user", "assistant"):
                    message = entry.get("message")
                    if isinstance(message, dict) and "content" in message:
                        content = message["content"]
                        messages.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
            text = "\n".join(messages)
            if text:
                ident = path.relative_to(self.history).as_posix()
                yield {"id": ident, "version": hashlib.sha256(data).hexdigest(), "cursor": ident, "text": text}


def _internal_embedding_address(endpoint):
    """Resolve once, reject any non-internal address, and return a pinned IP."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    from axcommon.embedding import EmbeddingUnavailable

    try:
        url = urlsplit(endpoint)
        host = url.hostname
        if (url.scheme not in ('http', 'https') or not host or url.username or url.password
                or url.query or url.fragment or not url.path.endswith('/v1/embeddings')):
            raise ValueError
        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
            if not ('.' not in host or host.endswith('.ts.net')):
                raise ValueError
        addresses = {ipaddress.ip_address(item[4][0]) for item in
                     socket.getaddrinfo(host, url.port or (443 if url.scheme == 'https' else 80),
                                        type=socket.SOCK_STREAM)}
        networks = tuple(ipaddress.ip_network(x) for x in
                         ('127.0.0.0/8', '::1/128', '10.0.0.0/8', '172.16.0.0/12',
                          '<existing-docker-range>', '100.64.0.0/10'))
        if not addresses or any(not any(a.version == n.version and a in n for n in networks)
                                for a in addresses):
            raise ValueError
        return url, str(sorted(addresses, key=lambda a: (a.version, int(a)))[0]), is_ip
    except (ValueError, OSError):
        raise EmbeddingUnavailable('embedding_endpoint_not_internal') from None


class InternalEmbedding:
    """Inspect destinations and pin resolved hostnames before sending text."""
    def __init__(self, delegate, *, transport=None):
        self.delegate = delegate
        self.transport = transport

    def validate_text(self, text):
        return self.delegate.validate_text(text)

    def embed(self, provider, model, text):
        import math
        import httpx
        from axcommon.embedding import EmbeddingUnavailable
        from urllib.parse import urlunsplit

        d = self.delegate
        # No DNS lookup or request precedes the secret and personal-data check.
        d.validate_text(text)
        url, address, is_ip = _internal_embedding_address(d.endpoint)
        if is_ip and self.transport is None:
            return d.embed(provider, model, text)
        try:
            if not (d.resources.registered_model(provider, model) and
                    d.resources.registered_embedding_endpoint(provider, model, d.endpoint)):
                raise EmbeddingUnavailable('unregistered_embedding_endpoint')
        except EmbeddingUnavailable:
            raise
        except Exception:
            raise EmbeddingUnavailable('unregistered_embedding_endpoint') from None
        if 'claude' in provider.casefold() or 'claude' in model.casefold() or 'anthropic' in provider.casefold():
            raise EmbeddingUnavailable('unsupported_provider')
        if type(d.dimensions) is not int or d.dimensions < 1:
            raise EmbeddingUnavailable('invalid_configuration')
        try:
            token = Path(d.token_file).read_text(encoding='utf-8').strip() if d.token_file else ''
        except (OSError, ValueError):
            token = ''
        if not token:
            raise EmbeddingUnavailable('credential_unavailable')
        address_part = f'[{address}]' if ':' in address else address
        port = f':{url.port}' if url.port is not None else ''
        pinned = urlunsplit((url.scheme, address_part + port, url.path, '', ''))
        try:
            with httpx.Client(transport=self.transport, timeout=d.timeout, follow_redirects=False,
                              trust_env=False) as client:
                response = client.post(pinned, headers={'Authorization': 'Bearer ' + token,
                                       'Host': url.netloc}, json={'model': model, 'input': text},
                                       extensions={'sni_hostname': url.hostname})
                response.raise_for_status()
                data = response.json()['data']
                if len(data) != 1 or data[0].get('index') != 0:
                    raise ValueError
                vector = data[0]['embedding']
                if not isinstance(vector, list):
                    raise ValueError
                if len(vector) != d.dimensions:
                    raise EmbeddingUnavailable('dimension_mismatch')
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
                    raise ValueError
                return vector
        except EmbeddingUnavailable:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, OverflowError):
            raise EmbeddingUnavailable('provider_unavailable_or_invalid_response') from None


class ConversationFactory:
    def __init__(self, data_dir, index_type, *, environ=None, transport=None, embedding_transport=None):
        self.embedding_transport = embedding_transport
        self.data_dir, self.index_type = Path(data_dir), index_type
        self.environ = os.environ if environ is None else environ
        self.transport = transport

    def open(self, ctx):
        from axcommon.client import service_url
        from axcommon.embedding import OpenAIEmbedding
        from core.resources.client import EmbeddingRegistryClient
        e = self.environ
        provider, model = e.get("AX_EMBEDDING_PROVIDER"), e.get("AX_EMBEDDING_MODEL")
        history = e.get("AX_CONVERSATION_HISTORY_DIR")
        if not provider or not model or not history or not Path(history).is_dir():
            raise ValueError("conversation_index_configuration_unavailable")
        resources = EmbeddingRegistryClient(service_url("resources", 8081, e), transport=self.transport)
        embedder = OpenAIEmbedding.from_environment(resources, environ=e)
        if (not embedder.endpoint or not embedder.token_file or embedder.dimensions < 1
                or not e.get("AX_REDACT_INDEX") or not e.get("AX_PII_LIST")
                or not embedder.redactor.index_ok or not embedder.redactor.pii_list_ok):
            raise ValueError("embedding_configuration_unavailable")
        if not (resources.registered_model(provider, model)
                and resources.registered_embedding_endpoint(provider, model, embedder.endpoint)):
            raise ValueError("embedding_endpoint_unregistered")
        _internal_embedding_address(embedder.endpoint)
        try:
            if not Path(embedder.token_file).read_text(encoding="utf-8").strip():
                raise ValueError("embedding_credential_unavailable")
        except OSError:
            raise ValueError("embedding_credential_unavailable") from None
        directory = self.data_dir / "conversation-index"
        directory.mkdir(parents=True, exist_ok=True)
        index = self.index_type(directory / "embeddings.sqlite3", ConversationSource(history), resources,
                                InternalEmbedding(embedder, transport=self.embedding_transport), redactor=embedder.redactor)
        return index, provider, model

    async def run_async(self, ctx):
        def run():
            index, provider, model = self.open(ctx)
            try:
                result = index.run(provider, model)
                if result.get("result") == "unavailable":
                    raise ValueError(result.get("reason", "conversation_index_unavailable"))
                return result
            finally:
                index.db.close()
        return await asyncio.to_thread(run)


class SecurityCredentialUnavailable(Exception):
    pass


class SecurityBaselineUnavailable(Exception):
    pass


def monitor_delivery(data_dir, environ, transport=None):
    """Use the HQ notification adapter only when a destination is configured."""
    endpoint = environ.get('AX_NTFY_ENDPOINT')
    if not endpoint:
        return None
    from axcommon.redact import from_environment
    from core.hq.notify.dispatcher import Dispatcher
    from core.hq.notify.guard import Guard
    from core.hq.notify.ntfy import NtfyClient
    from core.hq.notify.store import NotifyStore
    from plugins.timer.b16a_monitor.delivery import MonitorDelivery
    directory = Path(data_dir) / 'monitor-notifications'
    directory.mkdir(parents=True, exist_ok=True)
    store = NotifyStore(directory / 'notifications.sqlite3')
    sender = NtfyClient(endpoint, environ.get('AX_NTFY_TOPIC', 'hub'), transport=transport)
    return MonitorDelivery(Dispatcher(store, {'ntfy': sender}, guard=Guard(from_environment())))


class TumuWatchFactory:
    def __init__(self, data_dir, *, environ=None, reader=None, notifier=None):
        self.data_dir = Path(data_dir)
        self.environ = os.environ if environ is None else environ
        self.reader, self.notifier = reader, notifier

    def run(self, ctx):
        from plugins.timer.b16a_monitor import Monitor
        from plugins.timer.b16a_monitor.tumu import TumuSSHReader
        try:
            config = json.loads(Path(self.environ['AX_TUMU_TARGETS_FILE']).read_text(encoding='utf-8'))
            if not isinstance(config, dict) or not config or any(not isinstance(k, str) or not k for k in config):
                raise ValueError('invalid targets')
        except (KeyError, OSError, ValueError, UnicodeError):
            config = {'unconfigured': None}
        reader = self.reader or TumuSSHReader(config)
        notifier = self.notifier or monitor_delivery(self.data_dir, self.environ)
        class UnavailableNotifier:
            def send(self, *args):
                return 'failed'
        class Ledger:
            def record(self, kind, target, key, reason):
                import hashlib
                ident = 'monitor-' + hashlib.sha256(key.encode()).hexdigest()[:24]
                ctx.clients['ledger'].post('/v1/todos', {'id': ident,
                    'title': 'Monitor notification requires attention', 'state': 'todo', 'owner': 'system',
                    'reason': 'Notification delivery is not confirmed'})
        directory = self.data_dir / 'tumu-watch'
        directory.mkdir(parents=True, exist_ok=True)
        monitor = Monitor(str(directory / 'monitor.sqlite3'), tumu=reader, environment_jobs=None,
                          notifier=notifier or UnavailableNotifier(), ledger=Ledger())
        try:
            results = {target: monitor.poll_tumu(target) for target in config}
            return {'result': 'unavailable' if any(v['status'] != 'ok' for v in results.values()) else 'checked',
                    'targets': results}
        finally:
            monitor.db.close()
            if self.notifier is None and notifier and hasattr(notifier, 'dispatcher'):
                notifier.dispatcher.store.close()


class SecurityWatchFactory:
    def __init__(self, data_dir, *, environ=None, token_reader=None, transport=None):
        self.data_dir = Path(data_dir)
        self.environ = os.environ if environ is None else environ
        self.token_reader = token_reader
        self.transport = transport

    def run(self, ctx):
        import hashlib
        from axcommon import creds
        from axcommon.client import service_url
        from plugins.timer.b16a_monitor.environment_jobs import create_monitor
        reader = self.token_reader or creds.read_secret_optional
        token = reader('plugin-token-security-watch')
        if not isinstance(token, str) or not token.strip():
            raise SecurityCredentialUnavailable()
        path = Path(self.environ.get('AX_SECURITY_BASELINES_FILE') or self.data_dir / 'security-watch' / 'baselines.json')
        try:
            baselines = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(baselines, dict) or not baselines:
                raise ValueError('invalid baseline')
        except (OSError, ValueError, UnicodeError):
            raise SecurityBaselineUnavailable() from None
        delivery = monitor_delivery(self.data_dir, self.environ, self.transport)
        class Notifier:
            def send(self, key, kind, target, detail):
                ident = 'n-' + hashlib.sha256(key.encode()).hexdigest()[:20]
                ctx.emit('change.notification', ident, {'notification_id': ident, 'item_id': target,
                         'state': 'requested', 'attempt': 0, 'next_retry_at': None,
                         'title': kind, 'text': detail, 'level': 'alert'})
                # An accepted notification request does not establish delivery.
                return delivery.send(key, kind, target, detail) if delivery else 'unknown'
        class Ledger:
            def record(self, kind, target, key, reason):
                ident = 'monitor-' + hashlib.sha256(key.encode()).hexdigest()[:24]
                try:
                    ctx.clients['ledger'].post('/v1/todos', {
                        'id': ident, 'title': 'Monitor notification requires attention',
                        'state': 'todo', 'owner': 'system',
                        'reason': 'Notification delivery is not confirmed',
                    })
                finally:
                    ctx.emit('notification.failed', target, {'kind': kind, 'reason': reason or 'unknown'})
        directory = self.data_dir / 'security-watch'
        directory.mkdir(parents=True, exist_ok=True)
        monitor = create_monitor(str(directory / 'monitor.sqlite3'),
                                 resources_url=service_url('resources', 8081, self.environ), token=token,
                                 tumu=None, notifier=Notifier(), ledger=Ledger(), transport=self.transport)
        try:
            results = monitor.poll_security(baselines)
            return {'result': 'unavailable' if any(x.get('status') == 'unavailable' for x in results.values())
                    else 'checked', 'targets': results}
        finally:
            monitor.db.close()
            if delivery:
                delivery.dispatcher.store.close()


def job_client(plugin_id, name, manifests, *, environ=None, token_reader=None, transport=None):
    from axcommon import creds
    from axcommon.client import ApiClient, service_url

    env = os.environ if environ is None else environ
    reader = token_reader or creds.read_secret_optional
    capabilities = manifests.get(plugin_id, {}).get("memory_capabilities")
    if capabilities is None:
        secret = f"plugin-token-{plugin_id}"
    elif isinstance(capabilities, list) and all(
            isinstance(capability, str) and capability in ("memory.read", "memory.write") for capability in capabilities) and len(capabilities) == len(set(capabilities)):
        scopes = frozenset(capabilities)
        if scopes == {"memory.read"}:
            secret = f"plugin-token-memory-read--{plugin_id}"
        elif scopes == {"memory.write"}:
            secret = f"plugin-token-memory-write--{plugin_id}"
        elif scopes == {"memory.read", "memory.write"}:
            secret = f"plugin-token-{plugin_id}"
        else:
            secret = None
    else:
        # Unknown declarations must not fall back to an unrestricted token.
        secret = None
    token = reader(secret) if secret else None
    return ApiClient(service_url(name, 8081, env), token, transport=transport)


def main() -> int:
    import uvicorn
    import yaml
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from axcommon import creds, timeutil
    from axcommon.defaults import Defaults
    from axcommon.client import ApiClient, TimelineClient, service_url
    from axcommon.events import EventSink
    from axcommon.http import create_app, json_error, read_json

    from runtime.subloop.store import Store
    from .ports import ControlTimerPort
    from .runner import ACTIONS, TimerRunner, load_handler

    root = Path(os.environ.get("AX_HOME_IN_CONTAINER", "/ax"))
    data_dir = Path(os.environ.get("AX_DATA_DIR", "/data"))
    host = os.environ.get("AX_HOST", "unknown")
    pid = "timer-runner"
    reg = yaml.safe_load((root / "plugins" / "registry.yaml").read_text(encoding="utf-8")) or {}
    manifests, handlers = {}, {}
    for p in reg.get("plugins", []):
        if p.get("kind") != "timer":
            continue
        mp = root / p["path"]
        try:
            manifest = yaml.safe_load(mp.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("id") != p["id"] or manifest.get("kind") != "timer":
                raise ValueError("timer manifest mismatch")
            manifests[p["id"]] = manifest
        except (OSError, ValueError, yaml.YAMLError) as e:
            log.error("timer manifest %s を読めない: %s", p["id"], type(e).__name__)
            continue
        h = load_handler(mp.parent)
        if h is not None:
            handlers[p["id"]] = h

    def factory(plugin_id: str, name: str):
        return job_client(plugin_id, name, manifests)

    own_token = creds.read_secret_optional(f"plugin-token-{pid}")
    sink = EventSink(TimelineClient(service_url("timeline", 8081, os.environ), own_token), pid, host, data_dir / "outbox", subloop=pid)
    sinks = {pid: sink}

    def job_sink(plugin_id: str):
        if plugin_id not in sinks:
            sinks[plugin_id] = EventSink(TimelineClient(service_url("timeline", 8081, os.environ),
                                                      creds.read_secret_optional(f"plugin-token-{plugin_id}")),
                                       plugin_id, host, data_dir / "outbox" / plugin_id, subloop=plugin_id)
        return sinks[plugin_id]

    class DigestFactory:
        def open(self, ctx):
            from zoneinfo import ZoneInfo
            from axcommon.redact import from_environment
            aliases = json.loads((data_dir / "memory-digest" / "aliases.json").read_text(encoding="utf-8"))
            if (not isinstance(aliases, dict) or not aliases or any(
                    not isinstance(k, str) or not k or not isinstance(v, list) or not v
                    or any(not isinstance(x, str) or not x for x in v) for k, v in aliases.items())):
                raise ValueError("digest_aliases_invalid")
            redactor = from_environment()
            if not os.environ.get("AX_REDACT_INDEX") or not os.environ.get("AX_PII_LIST") or not redactor.index_ok or not redactor.pii_list_ok:
                raise ValueError("digest_redaction_unavailable")
            digest_type = handlers["memory-digest"].__globals__["MemoryDigest"]
            digest = digest_type(data_dir / "memory-digest", factory(ctx.plugin_id, "memory-connector"), aliases, redactor)
            effective = ctx.store.get_meta("effective:memory-digest", {})
            return digest, ctx.now().astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat(), effective.get("knobs", {})

        def close(self, instance):
            instance.db.close()

    port = ControlTimerPort(ApiClient(service_url("control", 8081, os.environ), own_token))
    store = Store(data_dir / "timer-runner.db")
    tr = TimerRunner(plugin_id=pid, store=store, sink=sink, handlers=handlers, manifests=manifests, client_factory=factory, timer_source=port,
                     actions={a: port.fire for a in ACTIONS}, sink_factory=job_sink, require_effective=True,
                     extra_clients={"memory-digest": {"memory_digest": DigestFactory()},
                                    "conversation-index": {"conversation_index": ConversationFactory(
                                        data_dir, handlers["conversation-index"].__globals__["ConversationIndex"])}
                                    if "conversation-index" in handlers else {},
                                    "security-watch": {"security_watch": SecurityWatchFactory(data_dir)},
                                    "tumu-watch": {"tumu_watch": TumuWatchFactory(data_dir)},
                                    **({"discord-forward": {"discord_forward": handlers["discord-forward"].__globals__["DiscordFactory"](data_dir), "collect_only": handlers["discord-forward"].__globals__["DiscordCollector"](data_dir)}} if "discord-forward" in handlers else {})})
    saved = store.get_meta("mode")
    if saved in MODES:
        tr.mode = saved
    apply_token = creds.read_secret_optional("control-apply-token")
    tasks: list[asyncio.Task] = []

    async def status(request: Request) -> JSONResponse:
        return JSONResponse(tr.status())

    async def apply(request: Request) -> JSONResponse:
        got = creds.parse_bearer(request.headers.get("authorization"))
        import hmac

        if not apply_token or not got or not hmac.compare_digest(got, apply_token):
            return json_error(401, "unauthorized", "control の資格が要る")
        data, err = await read_json(request)
        if err is not None:
            return err
        if not isinstance(data, dict) or data.get("mode") not in MODES or not isinstance(data.get("revision"), int):
            return json_error(422, "bad_request", "mode と revision が要る")
        return json_error(501, "timer_apply_target_undefined", "S-4.4/D-4.4: 共有 timer-runner の /apply は対象 ID が未定義。effective の定期照会で適用する")

    async def loop() -> None:
        next_poll = None
        poll_s = Defaults.load().get("control_poll_s")
        while True:
            try:
                if next_poll is None or timeutil.now() >= next_poll:
                    for target in sorted(set(manifests) | {pid}):
                        try:
                            effective = await asyncio.to_thread(factory(target, "control").get, f"/v1/plugins/{target}/effective")
                            tr.set_effective(target, effective)
                        except Exception as e:  # noqa: BLE001  読めない対象だけ前回値を使う。
                            log.warning("effective %s を読めない: %s", target, type(e).__name__)
                    next_poll = timeutil.parse_ts(timeutil.add_seconds(timeutil.now_ts(), poll_s))
                await tr.tick()
                for outbox_sink in tuple(sinks.values()):
                    await asyncio.to_thread(outbox_sink.flush)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("tick の失敗: %s", type(e).__name__)
            await asyncio.sleep(float(os.environ.get("AX_TIMER_TICK_S", "5")))

    async def startup() -> None:
        tasks.append(asyncio.create_task(loop()))

    async def shutdown() -> None:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await tr.close()
        store.close()

    app = create_app(pid, "1.0.0", routes=[Route("/status", status, methods=["GET"]), Route("/apply", apply, methods=["POST"])], sink=sink,
                     extra_health=lambda: {"mode": tr.mode, "jobs": sorted(manifests)}, on_startup=[startup], on_shutdown=[shutdown])
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("AX_HEALTH_PORT", "8080")), log_level="warning")  # noqa: S104
    return 0


if __name__ == "__main__":
    sys.exit(main())

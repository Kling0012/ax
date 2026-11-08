"""Timer entry for Discord forwarding."""
import asyncio


async def run(ctx):
    if 'discord_forward' not in ctx.clients:
        return await asyncio.to_thread(ctx.clients['collect_only'].collect, ctx)
    return await asyncio.to_thread(ctx.clients['discord_forward'].run, ctx)


class DiscordFactory:
    """Keep the forwarder and its boot-time cutoff for one runner lifetime."""

    def __init__(self, data_dir, *, environ=None, decoder=None, source=None, webhook=None, clock=None):
        import os
        from pathlib import Path
        from axcommon import timeutil
        self.directory = Path(data_dir) / 'discord-forward'
        self.environ = os.environ if environ is None else environ
        self.decoder, self.source, self.webhook = decoder, source, webhook
        self.clock = clock or timeutil.now
        self.started_at = self.clock()
        self.engine = None
        self.ctx = None

    @staticmethod
    def _modules():
        import importlib.util
        import sys
        from pathlib import Path
        name = 'ax_discord_forward_runtime'
        if name not in sys.modules:
            folder = Path(__file__).parent
            spec = importlib.util.spec_from_file_location(name, folder / '__init__.py', submodule_search_locations=[str(folder)])
            package = importlib.util.module_from_spec(spec)
            sys.modules[name] = package
            spec.loader.exec_module(package)
        return sys.modules[name]

    def _targets(self, ctx):
        from core.intake.dest.b16b.common import TargetRegistry
        from pathlib import Path
        registry = TargetRegistry.from_client(ctx.clients['control'])
        path = Path(self.environ.get('AX_DISCORD_TARGETS_FILE') or self.directory / 'targets.yaml')
        targets = self._modules().load_targets(path, registry) if path.is_file() else []
        return registry, targets

    def collect(self, ctx):
        # The runner gives this path only a read-only collector, not the sender.
        # Supply control through the factory, never through a sender client.
        from axcommon.client import ApiClient, service_url
        from axcommon.creds import read_secret_optional
        from types import SimpleNamespace
        control = ApiClient(service_url('control', 8081, self.environ), read_secret_optional('plugin-token-discord-forward'))
        view = SimpleNamespace(clients={'control': control})
        _, targets = self._targets(view)
        ctx.emit('automation.targets', 'discord-forward', {'targets': [t.delivery_target() for t in targets]})
        return {'result': 'listed'}

    def run(self, ctx):
        from axcommon.client import ApiClient, service_url
        from axcommon.creds import read_secret_optional
        from axcommon.redact import from_environment
        from types import SimpleNamespace
        self.ctx = ctx
        control = ApiClient(service_url('control', 8081, self.environ), read_secret_optional('plugin-token-discord-forward'))
        if self.engine is None:
            package = self._modules()
            registry, targets = self._targets(SimpleNamespace(clients={'control': control}))
            redactor = from_environment(self.environ)
            if (not self.environ.get('AX_REDACT_INDEX') or not self.environ.get('AX_PII_LIST')
                    or not redactor.index_ok or not redactor.pii_list_ok):
                raise ValueError('discord_secret_scan_unavailable')
            source = self.source or package.UserPostsSource(_EnvironmentRunner(ctx.clients['resources']), decoder=self.decoder)
            from axcommon.creds import read_secret
            secret_dir = self.environ.get('AX_DISCORD_SECRETS_DIR', '/run/discord-webhooks')
            webhook = self.webhook or package.DiscordWebhook(secrets=lambda name: read_secret(name, base=secret_dir))
            first = [True]
            def boot_clock():
                if first[0]:
                    first[0] = False
                    return self.started_at
                return self.clock()
            def lost(record):
                import hashlib
                key = '\0'.join(record[k] for k in ('plugin_id', 'destination', 'source_item_id'))
                ident = hashlib.sha256(key.encode()).hexdigest()[:24]
                self.ctx.clients['ledger'].post('/v1/todos', {'id': 'discord-' + ident,
                    'title': 'Discord delivery requires reconciliation', 'state': 'todo', 'owner': 'system',
                    'reason': 'Delivery outcome is unknown'})
            self.engine = package.DiscordForwarder(self.directory, targets, registry, _ReadPermission(control), source,
                webhook, secret_scan=lambda text: not redactor.scan(text),
                emit=lambda *args: self.ctx.emit(*args), lost=lost, clock=boot_clock)
        return {'result': 'checked', 'targets': self.engine.tick(mode='run')}


class _ReadPermission:
    def __init__(self, client):
        self.client = client

    def check(self, target, operation):
        try:
            params = {k: target[k] for k in ("project", "repo", "destination")}
            result = self.client.get('/v1/permissions/check', params={**params, 'operation': operation})
            return isinstance(result, dict) and result.get('allowed') is True
        except Exception:
            return False


class _EnvironmentRunner:
    def __init__(self, client):
        self.client = client

    def run(self, request, artifacts):
        import time
        from types import SimpleNamespace
        if artifacts:
            raise ValueError('unexpected artifacts')
        self.client.post('/v1/env/jobs', request)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = self.client.get('/v1/env/jobs/' + request['job_id'])
            if result.get('state') in ('succeeded', 'failed', 'unknown'):
                values = list(result.get('results', {}).values())
                output = values[0].get('output', {}) if len(values) == 1 else {}
                return SimpleNamespace(state=result['state'], output=output)
            time.sleep(1)
        raise TimeoutError('environment_job_timeout')


class DiscordCollector:
    """Read-only target listing; no sending client is constructed or exposed."""
    _modules = staticmethod(DiscordFactory._modules)
    _targets = DiscordFactory._targets
    collect = DiscordFactory.collect

    def __init__(self, data_dir, *, environ=None):
        import os
        from pathlib import Path
        self.directory = Path(data_dir) / 'discord-forward'
        self.environ = os.environ if environ is None else environ

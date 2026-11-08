"""Exercise review through the public release service with injected adapters."""
from plugins.subloop.release.gate.integration import PersistentReleaseGate
from plugins.subloop.release.core.service import ReleaseService
from plugins.subloop.release.gate.publication import PublicationGate, SelectedReviewer


class Source:
    def __init__(self):
        self.history = 'old history'
        self.calls = []

    def collect(self, product, sha, notes):
        self.calls.append(sha)
        return {'README': 'release material', 'notes': str(notes)}, self.history


class Scanner:
    def __init__(self):
        self.calls = []

    def scan(self, text):
        self.calls.append(text)
        return ['hit'] if 'dummy-sensitive-history' in text else []


class Reviews:
    def __init__(self):
        self.count = 0
        self.outcome = 'unsure'

    def select(self, request):
        self.count += 1
        return SelectedReviewer(request.stage, lambda payload: {'outcome': self.outcome})


class Records:
    def append(self, record):
        return 'evidence:' + str(record['attempt'])


def setup_gate(service, ports):
    source, scanner, reviews = Source(), Scanner(), Reviews()
    gate = PublicationGate(redactor=scanner, pii_list_available=True,
                           internal_information_check=lambda t: True,
                           image_gps_check=lambda t: True, selector=reviews, records=Records())
    wired = ReleaseService(service.registry, service.store, ports,
                           publication_gate=PersistentReleaseGate(gate, source, service.store))
    service.ports = wired.ports
    return source, scanner, reviews


def test_t37_release_blocks_leak_and_does_not_publish(service, dream_batch, ports):
    source, scanner, reviews = setup_gate(service, ports)
    source.history = 'dummy-sensitive-history'
    service.plan(dream_batch['batch_id'], latest_tag='v1.2.3',
                 changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    result = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert result['reason'] == 'information_leak_detected'
    assert not any(call[0] == 'publish' for call in ports.calls)
    assert not reviews.count
    assert 'dummy-sensitive-history' in scanner.calls[0]


def test_t37_release_retries_full_review_then_publishes(service, dream_batch, ports):
    source, scanner, reviews = setup_gate(service, ports)
    service.plan(dream_batch['batch_id'], latest_tag='v1.2.3',
                 changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    first = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert first['reason'] == 'publication_gate_failed'
    reviews.outcome = 'ok'
    second = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert second['outcome'] == 'success'
    assert len(scanner.calls) == 2 and len(source.calls) == 2 and reviews.count == 6
    assert any(call[0] == 'publish' for call in ports.calls)


def test_t37_attempt_limit_persists_across_adapter_recreation(service, dream_batch, ports):
    source, scanner, reviews = setup_gate(service, ports)
    service.plan(dream_batch['batch_id'], latest_tag='v1.2.3',
                 changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    for _ in range(4):
        service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert len(source.calls) == 3 and len(scanner.calls) == 3
    assert not any(call[0] == 'publish' for call in ports.calls)


def test_public_graph_rejects_leak_before_publication(service, dream_batch, ports, tmp_path, repo_root, fake_clock):
    import asyncio
    from runtime.subloop.graph import load_yaml
    from runtime.subloop.inbox import accept_item
    from tests.fakes.b7 import make_env
    from tests.b7.test_t11_tdd import item

    source, _, _ = setup_gate(service, ports)
    source.history = 'dummy-sensitive-history'
    service.plan(dream_batch['batch_id'], latest_tag='v1.2.3',
                 changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    env = make_env(tmp_path, repo_root, fake_clock)
    plugin = repo_root / 'plugins/subloop/release'
    env.svc.release_service = service
    ex = env.executor(load_yaml(plugin / 'graph.yaml'), load_yaml(plugin / 'manifest.yaml'),
                      extra_nodes=[plugin / 'nodes'])
    ex.apply('run', {'dry_run': False}, 1)

    async def go():
        assert accept_item(ex, item(item_key=dream_batch['batch_id']))[0] == 200
        await ex.run_once()
        await ex.drain()

    asyncio.run(go())
    assert service.store.batch(dream_batch['batch_id'])['last_result']['reason'] == 'information_leak_detected'
    assert not any(call[0] == 'publish' for call in ports.calls)


def test_production_graph_holds_when_dependencies_are_unavailable(tmp_path, repo_root, fake_clock):
    import asyncio
    from runtime.subloop.config import build_services
    from runtime.subloop.graph import load_yaml
    from runtime.subloop.inbox import accept_item
    from tests.b15.conftest import merged
    from tests.fakes.b7 import make_env
    from tests.b7.test_t11_tdd import item
    from tests.fakes.b15.release.ports import SHA

    env = make_env(tmp_path, repo_root, fake_clock)
    svc = build_services({'AX_PLUGIN_ID': 'release', 'AX_DATA_DIR': str(tmp_path / 'prod')},
                         repo_root=repo_root)
    release = svc.release_service
    release.sink = env.svc.sink
    release.ingest(**merged())
    batch = release.store.form_batch(release.registry.get('dreamforge'), SHA, force=True)
    release.plan(batch['batch_id'], latest_tag='v1.2.3',
                 changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    env.svc.release_service = release
    plugin = repo_root / 'plugins/subloop/release'
    ex = env.executor(load_yaml(plugin / 'graph.yaml'), load_yaml(plugin / 'manifest.yaml'),
                      extra_nodes=[plugin / 'nodes'])
    ex.apply('run', {'dry_run': False}, 1)

    async def go():
        assert accept_item(ex, item(item_key=batch['batch_id']))[0] == 200
        await ex.run_once()
        await ex.drain()

    asyncio.run(go())
    result = release.store.batch(batch['batch_id'])['last_result']
    assert result['reason'] == 'credentials_unverified'
    assert result['outcome'] == 'disabled'
    assert release.store.operation('release:dreamforge:v1.2.4') is None


def test_production_builder_scans_and_blocks_secrets_before_publication(tmp_path, repo_root, fake_clock, monkeypatch):
    import asyncio
    from plugins.subloop.release.core.production import build_release_service
    from runtime.subloop.graph import load_yaml
    from runtime.subloop.inbox import accept_item
    from tests.b15.conftest import merged
    from tests.fakes.b15.release.ports import FakeReleasePorts, SHA
    from tests.fakes.b7 import make_env
    from tests.b7.test_t11_tdd import item

    pii, hosts, terms = (tmp_path / name for name in ('pii', 'hosts', 'terms'))
    pii.write_text('fixture-private-phrase\n')
    hosts.write_text('internal.example\n')
    terms.write_text('internal-example-term\n')
    monkeypatch.setattr('plugins.subloop.release.gate.scanners.shutil.which', lambda _: None)
    env = make_env(tmp_path, repo_root, fake_clock)
    ports, source = FakeReleasePorts(), Source()
    source.history = 'fixture-private-phrase'
    release = build_release_service(repo_root, tmp_path / 'release-prod', 'test', env.svc.sink,
                                    pii_path=str(pii), hosts_path=str(hosts), terms_path=str(terms),
                                    source=source, ports=ports)
    release.ingest(**merged())
    batch = release.store.form_batch(release.registry.get('dreamforge'), SHA, force=True)
    release.plan(batch['batch_id'], latest_tag='v1.2.3',
                 changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    env.svc.release_service = release
    plugin = repo_root / 'plugins/subloop/release'
    ex = env.executor(load_yaml(plugin / 'graph.yaml'), load_yaml(plugin / 'manifest.yaml'),
                      extra_nodes=[plugin / 'nodes'])
    ex.apply('run', {'dry_run': False}, 1)
    async def go():
        assert accept_item(ex, item(item_key=batch['batch_id']))[0] == 200
        await ex.run_once()
        await ex.drain()
    asyncio.run(go())
    result = release.store.batch(batch['batch_id'])['last_result']
    assert result['reason'] == 'information_leak_detected'
    assert source.calls and not any(call[0] == 'publish' for call in ports.calls)


def test_public_graph_review_adapters_and_repository_boundaries(tmp_path, repo_root, fake_clock, monkeypatch):
    import asyncio
    import json
    import subprocess
    import yaml
    from plugins.subloop.release.core.production import build_release_service
    from plugins.subloop.release.gate.adapters import GatewayReviews
    from runtime.subloop.graph import load_yaml
    from runtime.subloop.inbox import accept_item
    from tests.b15.conftest import merged
    from tests.fakes.b15.release.ports import FakeReleasePorts
    from tests.fakes.b7 import make_env
    from tests.b7.test_t11_tdd import item

    monkeypatch.setattr('plugins.subloop.release.gate.scanners.shutil.which', lambda _: None)
    repo = tmp_path / 'repository'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture')
    (repo / 'README').write_text('safe fixture material')
    git('add', 'README')
    git('commit', '-m', 'fixture')
    sha = git('rev-parse', 'HEAD')
    pii, hosts, terms = (tmp_path / name for name in ('pii', 'hosts', 'terms'))
    pii.write_text('fixture-private-phrase\n')
    hosts.write_text('')
    terms.write_text('')
    allow = tmp_path / 'allow.yaml'

    class Resources:
        def __init__(self):
            self.held = set()
        def get(self, path, **kwargs):
            return {'resources': [
                {'id': 'anthropic/claude-example', 'family': 'anthropic', 'kind': 'model', 'status': 'active', 'level': 'L3', 'tier': 'free'},
                {'id': 'fake/reviewer', 'family': 'fake', 'kind': 'model', 'status': 'active', 'level': 'L3', 'tier': 'free'},
            ]}
        def post(self, path, body):
            if path.endswith('/reserve'):
                self.held.add(body['reservation_id'])
                return {'reserved': True}
            if body.get('outcome') == 'unknown':
                return {'unknown': body['reservation_id'] in self.held}
            self.held.discard(body['reservation_id'])
            return {'released': True}

    class Gateway:
        def __init__(self, outcome):
            self.outcome, self.calls = outcome, []
        def get(self, path):
            models = [{'id': 'anthropic/claude-example'}]
            if self.outcome != 'claude-only':
                models.append({'id': 'fake/reviewer'})
            return {'data': models}
        def post(self, path, body):
            self.calls.append(body)
            if self.outcome is None:
                raise TimeoutError()
            return {'choices': [{'message': {'content': json.dumps({'outcome': self.outcome})}}]}

    def run_case(label, outcome='ok', visibility='PUBLIC', allowed=True, secret=False):
        if secret:
            (repo / 'README').write_text('fixture-private-phrase')
            git('add', 'README')
            git('commit', '-m', 'fixture two')
        current_sha = git('rev-parse', 'HEAD')
        ports = FakeReleasePorts(sha=current_sha)
        (tmp_path / label).mkdir()
        env = make_env(tmp_path / label, repo_root, fake_clock)
        gateway = Gateway(outcome)
        resources = Resources()
        reviews = GatewayReviews(resources, gateway, repo_root / 'config/roles.yaml')
        release = build_release_service(repo_root, tmp_path / (label + '-data'), 'test', env.svc.sink,
                                        pii_path=pii, hosts_path=hosts, terms_path=terms,
                                        repositories_path=allow, select_model=reviews.select,
                                        execute_model=reviews.execute, ports=ports)
        product = release.registry.get('dreamforge')
        allow.write_text(yaml.safe_dump({'repositories': [{'repo': product['repo'] if allowed else 'other/repo',
                                                          'visibility': visibility, 'path': str(repo)}]}))
        release.ingest(**merged())
        batch = release.store.form_batch(product, current_sha, force=True)
        release.plan(batch['batch_id'], latest_tag='v1.2.3',
                     changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
        env.svc.release_service = release
        plugin = repo_root / 'plugins/subloop/release'
        ex = env.executor(load_yaml(plugin / 'graph.yaml'), load_yaml(plugin / 'manifest.yaml'), extra_nodes=[plugin / 'nodes'])
        ex.apply('run', {'dry_run': False}, 1)
        async def go():
            assert accept_item(ex, item(item_key=batch['batch_id']))[0] == 200
            await ex.run_once()
            await ex.drain()
        asyncio.run(go())
        result = release.store.batch(batch['batch_id'])['last_result']
        return result, ports.calls, gateway.calls, resources.held

    result, calls, sent, held = run_case('pass')
    assert result['outcome'] == 'success' and any(c[0] == 'publish' for c in calls), (result, calls, sent)
    assert len(sent) >= 3 and all(c['model'] == 'fake/reviewer' for c in sent)
    assert not held
    result, calls, sent, held = run_case('private', visibility='PRIVATE')
    assert result['outcome'] == 'success' and all('\"sensitivity\": \"private\"' in c['messages'][1]['content'] for c in sent)
    for label, outcome in [('ng', 'ng'), ('timeout', None), ('unsure', 'unsure')]:
        result, calls, sent, held = run_case(label, outcome)
        assert result['reason'] == 'publication_gate_failed' and not any(c[0] == 'publish' for c in calls)
        assert bool(held) == (outcome is None)
    result, calls, sent, held = run_case('claude-only', outcome='claude-only')
    assert result['reason'] == 'publication_gate_failed' and not sent
    for label, visibility, allowed in [('unlisted', 'PUBLIC', False), ('unknown', 'UNKNOWN', True)]:
        result, calls, sent, held = run_case(label, visibility=visibility, allowed=allowed)
        assert result['reason'] == 'publication_gate_failed' and not sent and not any(c[0] == 'publish' for c in calls)
    result, calls, sent, held = run_case('secret', secret=True)
    assert result['reason'] == 'information_leak_detected' and not sent and not any(c[0] == 'publish' for c in calls)

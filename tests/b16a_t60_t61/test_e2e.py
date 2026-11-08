import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from axcommon.redact import Redactor
from runtime.timer_runner.runner import load_handler

ROOT = Path(__file__).resolve().parents[2]


def plugin(name):
    path = ROOT / 'plugins' / 'timer' / name / 'handler.py'
    spec = importlib.util.spec_from_file_location('test_' + name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke(name, key, values):
    class Factory:
        def open(self, ctx):
            return values
    ctx = SimpleNamespace(clients={key: Factory()})
    return asyncio.run(load_handler(ROOT / 'plugins' / 'timer' / name)(ctx))


def test_conversation_index_increment_recovery_and_outbound_boundary(tmp_path):
    module = plugin('conversation-index')
    token = 'sk-' + 'A' * 25
    address = 'test-user-8@example.com'
    rows = [
        {'id': 'a', 'version': '1', 'cursor': 1, 'text': 'private alpha ' + token + ' ' + address},
        {'id': 'b', 'version': '1', 'cursor': 2, 'text': 'private beta'},
    ]
    class Source:
        def changes(self, cursor):
            return iter(rows[cursor or 0:])
    class Resources:
        def registered_model(self, provider, model):
            return (provider, model) == ('registered', 'model')
    class Embedder:
        def __init__(self):
            self.calls = []
            self.fail = True
        def embed(self, provider, model, text):
            self.calls.append(text)
            if 'beta' in text and self.fail:
                raise RuntimeError('offline')
            return [1.0]
    embed = Embedder()
    index = module.ConversationIndex(tmp_path / 'index.db', Source(), Resources(), embed, Redactor(allow_email_domains=[]))
    with pytest.raises(PermissionError):
        invoke('conversation-index', 'conversation_index', (index, 'unknown', 'model'))
    assert embed.calls == []
    with pytest.raises(RuntimeError):
        invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))
    assert len(index.search('alpha')) == 1
    index.db.close()
    embed.fail = False
    index = module.ConversationIndex(tmp_path / 'index.db', Source(), Resources(), embed, Redactor(allow_email_domains=[]))
    invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))
    invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))
    assert len(index.search('beta')) == 1
    assert sum('alpha' in x for x in embed.calls) == 1
    assert sum('beta' in x for x in embed.calls) == 2
    assert all(token not in x and address not in x for x in embed.calls)
    rows.append({'id': 'a', 'version': '2', 'cursor': 3, 'text': 'private alpha changed'})
    invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))
    assert index.search('changed')[0]['version'] == '2'
    assert len(index.search('alpha')) == 1
    assert module.schedule({'u_index_time': '05:03', 'enabled': False}) == {'enabled': False, 'kind': 'cron', 'cron': '3 20 * * *'}


def test_memory_digest_read_only_outputs_and_settings(tmp_path):
    module = plugin('memory-digest')
    token = 'sk-' + 'B' * 25
    address = 'test-user-8@example.com'
    class Memory:
        def __init__(self):
            self.calls = []
        def get(self, path, params):
            self.calls.append((path, dict(params)))
            if 'cursor' not in params:
                data = {'items': [{'memory_id': '1', 'created_at': '2026-11-07T15:10:00Z', 'content': 'Hibi work ' + token + ' ' + address, 'tags': ['hibi']}], 'next_cursor': 'next'}
            else:
                data = {'items': [{'memory_id': '2', 'created_at': '2026-11-08T02:00:00Z', 'content': 'Dreamforge update', 'tags': []}]}
            return {'memories': {'structuredContent': data}}
    memory = Memory()
    digest = module.MemoryDigest(tmp_path, memory, {'Hibi': ['hibi'], 'Dreamforge': ['dreamforge']}, Redactor(allow_email_domains=[]))
    result = invoke('memory-digest', 'memory_digest', (digest, '2026-11-08', {'u_digest_time': '20:15', 'enabled': True}))
    assert set(result['artifacts']) == {'daily', 'project:Hibi', 'project:Dreamforge'}
    contents = [Path(p).read_text() for p in result['artifacts'].values()]
    assert 'Hibi' in contents[0] and 'Dreamforge' in contents[0]
    assert all(token not in text and address not in text for text in contents)
    assert module.schedule({'u_digest_time': '20:15', 'enabled': True})['cron'] == '15 11 * * *'
    repeat = invoke('memory-digest', 'memory_digest', (digest, '2026-11-08', {}))
    assert repeat['artifacts'] == result['artifacts']
    before = len(memory.calls)
    assert invoke('memory-digest', 'memory_digest', (digest, '2026-11-08', {'enabled': False}))['result'] == 'disabled'
    assert len(memory.calls) == before
    assert all(path == '/v1/memories' for path, _ in memory.calls)
    old_paths = result['artifacts']
    old_contents = {k: Path(v).read_text() for k, v in old_paths.items()}
    class BrokenMemory:
        def get(self, path, params):
            raise RuntimeError('offline')
    digest.memory = BrokenMemory()
    with pytest.raises(RuntimeError):
        invoke('memory-digest', 'memory_digest', (digest, '2026-11-08', {}))
    assert {k: Path(v).read_text() for k, v in old_paths.items()} == old_contents


def test_production_history_local_index_never_sends_raw_conversation(tmp_path, monkeypatch):
    import json
    import sqlite3
    import subprocess
    import urllib.request

    source = tmp_path / 'history'
    source.mkdir()
    state = tmp_path / 'state'
    state.mkdir()
    secret_index = tmp_path / 'secret-index'
    secret_index.write_text('salt\n')
    pii = tmp_path / 'pii'
    pii.write_text('')
    (tmp_path / 'redact.yaml').write_text('allow_email_domains: [example.org]\n')
    secret = 'sk-' + 'Z' * 25
    email = 'test-user-8@example.com'
    transcript = source / 'conversation.jsonl'
    transcript.write_text(json.dumps({'type': 'user', 'message': {'content': 'alpha ' + secret + ' ' + email}}) + '\n')
    for key, value in {'AX_CONVERSATION_HISTORY_DIR': source, 'AX_DATA_DIR': state,
                       'AX_REDACT_INDEX': secret_index, 'AX_PII_LIST': pii, 'AX_CONFIG_DIR': tmp_path}.items():
        monkeypatch.setenv(key, str(value))
    def forbidden(*args, **kwargs):
        raise AssertionError('external index or transmission invoked')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    async def invoke_local():
        handler = load_handler(ROOT / 'plugins' / 'timer' / 'conversation-index')
        return await handler(SimpleNamespace(clients={}))
    assert asyncio.run(invoke_local())['external_index'] == 'disabled'
    db_path = state / 'conversation-index' / 'index.sqlite3'
    with sqlite3.connect(db_path) as db:
        text = db.execute('SELECT text FROM docs').fetchone()[0]
    assert 'alpha' in text and secret not in text and email not in text
    assert asyncio.run(invoke_local())['result'] == 'indexed_local'
    transcript.write_text(json.dumps({'type': 'user', 'message': {'content': 'beta'}}) + '\n')
    assert asyncio.run(invoke_local())['result'] == 'indexed_local'
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT count(*), max(text) FROM docs').fetchone() == (1, 'beta')
    pii.unlink()
    assert asyncio.run(invoke_local())['reason'] == 'redaction_unavailable'


def test_real_embedding_adapter_outbound_and_failure_isolation(tmp_path, monkeypatch):
    import _socket
    import socket
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from axcommon.embedding import OpenAIEmbedding

    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            body = json.dumps({'data': [{'index': 0, 'embedding': [0.5, 0.25]}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    def local_connect(sock, address):
        if address != ('127.0.0.1', server.server_port):
            raise RuntimeError('external network forbidden')
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    index_file = tmp_path / 'redact-index'
    pii_file = tmp_path / 'pii-list'
    token_file = tmp_path / 'token'
    index_file.write_text('testsalt\n')
    pii_file.write_text('private person\n')
    token_file.write_text('test-token')
    env = {'AX_EMBEDDING_URL': f'http://127.0.0.1:{server.server_port}/v1/embeddings',
           'AX_EMBEDDING_TOKEN_FILE': str(token_file), 'AX_EMBEDDING_DIMENSIONS': '2',
           'AX_REDACT_INDEX': str(index_file), 'AX_PII_LIST': str(pii_file)}
    class Resources:
        def registered_model(self, provider, model):
            return (provider, model) == ('registered', 'model')
    rows = [{'id': 'a', 'version': '1', 'cursor': 1, 'text': 'safe text'}]
    class Source:
        def changes(self, cursor):
            return iter(rows[cursor or 0:])
    resources = Resources()
    resources.registered_embedding_endpoint = lambda provider, model, endpoint: endpoint == env['AX_EMBEDDING_URL']
    adapter = OpenAIEmbedding.from_environment(resources, env)
    module = plugin('conversation-index')
    index = module.ConversationIndex(tmp_path / 'real.db', Source(), resources, adapter, adapter.redactor)
    try:
        adapter.endpoint = f'http://127.0.0.1:{server.server_port}/unregistered/v1/embeddings'
        assert invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))['reason'] == 'unregistered_embedding_endpoint'
        assert requests == []
        adapter.endpoint = env['AX_EMBEDDING_URL']
        assert invoke('conversation-index', 'conversation_index', (index, 'registered', 'model')) == {'result': 'indexed'}
        assert requests == [('/v1/embeddings', {'model': 'model', 'input': 'safe text'})]
        assert index.search('safe')[0]['id'] == 'a'
        rows.append({'id': 'b', 'version': '1', 'cursor': 2, 'text': 'secret sk-' + 'A' * 25})
        assert invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))['reason'] == 'sensitive_text'
        assert len(requests) == 2
        assert index.search('safe')['result'] == 'fallback'
        rows[1]['text'] = 'private person'
        assert invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))['reason'] == 'sensitive_text'
        assert len(requests) == 2
        rows[1]['text'] = 'ordinary next text'
        adapter.dimensions = 3
        assert invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))['reason'] == 'dimension_mismatch'
        adapter.dimensions = 2
        adapter.token_file = str(tmp_path / 'missing')
        assert invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))['reason'] == 'credential_unavailable'
        adapter.token_file = str(token_file)
        server.shutdown()
        thread.join()
        assert invoke('conversation-index', 'conversation_index', (index, 'registered', 'model'))['result'] == 'unavailable'
        assert index.search('safe')['result'] == 'fallback'
        # Independent plugin remains usable while embedding is offline.
        assert module.schedule({'enabled': True})['enabled'] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        index.db.close()


def test_semantic_search_and_safe_fallback_through_public_index(tmp_path, monkeypatch):
    import _socket
    import socket
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from axcommon.embedding import OpenAIEmbedding

    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['input']
            calls.append(value)
            vector = [1, 0] if value in ('first document', 'query') else [0, 1]
            body = json.dumps({'data': [{'index': 0, 'embedding': vector}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    def local_connect(sock, address):
        if address != ('127.0.0.1', server.server_port):
            raise RuntimeError('external network forbidden')
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    index_file, pii_file, token_file = (tmp_path / name for name in ('redact-index', 'pii-list', 'token'))
    index_file.write_text('salt\n')
    pii_file.write_text('')
    token_file.write_text('test-token')
    endpoint = f'http://127.0.0.1:{server.server_port}/v1/embeddings'
    class Resources:
        def registered_model(self, provider, model):
            return (provider, model) == ('provider', 'model')
        def registered_embedding_endpoint(self, provider, model, url):
            return self.registered_model(provider, model) and url == endpoint
    entries = [{'id': 'a', 'version': '1', 'cursor': 1, 'text': 'first document'},
               {'id': 'b', 'version': '1', 'cursor': 2, 'text': 'second document'}]
    class Source:
        def changes(self, cursor):
            return iter(entries[cursor or 0:])
    resources = Resources()
    adapter = OpenAIEmbedding.from_environment(resources, {'AX_EMBEDDING_URL': endpoint,
        'AX_EMBEDDING_TOKEN_FILE': str(token_file), 'AX_EMBEDDING_DIMENSIONS': '2',
        'AX_REDACT_INDEX': str(index_file), 'AX_PII_LIST': str(pii_file)})
    index = plugin('conversation-index').ConversationIndex(tmp_path / 'index.db', Source(), resources, adapter, adapter.redactor)
    try:
        assert invoke('conversation-index', 'conversation_index', (index, 'provider', 'model'))['result'] == 'indexed'
        result = index.search('query')
        assert [item['id'] for item in result] == ['a', 'b']
        assert result[0]['score'] > result[1]['score']
        before = len(calls)
        secret = 'sk-' + 'C' * 25
        assert index.search('first ' + secret)['reason'] == 'sensitive_text'
        assert len(calls) == before
        entries.append({'id': 'c', 'version': '1', 'cursor': 3, 'text': secret})
        assert invoke('conversation-index', 'conversation_index', (index, 'provider', 'model'))['reason'] == 'sensitive_text'
        result = index.search('first')
        assert result['result'] == 'fallback' and result['reason'] == 'sensitive_text'
        assert [item['id'] for item in result['items']] == ['a']
        assert len(calls) == before
    finally:
        index.db.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_timer_factory_isolates_missing_and_unregistered_embedding(tmp_path, monkeypatch):
    import json
    import yaml
    from starlette.testclient import TestClient
    from core.resources.app import Config, build
    from runtime.subloop.store import Store
    from runtime.timer_runner.__main__ import ConversationFactory
    from runtime.timer_runner.runner import TimerRunner

    history = tmp_path / 'history'
    history.mkdir()
    (history / 'session.jsonl').write_text(json.dumps({'type': 'user', 'message': {'content': 'hello'}}) + '\n')
    for name, text in [('index', 'salt\n'), ('pii', 'private-test\n'), ('token', 'test-token')]:
        (tmp_path / name).write_text(text)
    models = tmp_path / 'models.yaml'
    models.write_text(yaml.safe_dump({'models': [{'id': 'provider/model', 'kind': 'model',
        'family': 'provider', 'tier': 'standard', 'status': 'active',
        'embedding_endpoint': 'https://127.0.0.1/v1/embeddings'}]}))
    core, _, _ = build(Config(db=str(tmp_path / 'resources.db'), models_file=str(models)), run_heartbeat_loop=False)
    import httpx
    network = []
    def fake_post(url, **kwargs):
        network.append((url, kwargs['json']))
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [0.5]}]},
                              request=httpx.Request('POST', url))
    monkeypatch.setattr('httpx.post', fake_post)
    class Sink:
        def emit(self, *args, **kwargs):
            return None
    store = Store(tmp_path / 'timer.db')
    index_module = plugin('conversation-index')
    executed = []
    async def other(ctx):
        executed.append(ctx.plugin_id)
        return {'result': 'ok'}
    handlers = {'conversation-index': load_handler(ROOT / 'plugins/timer/conversation-index'), 'other': other}
    manifests = {name: {'id': name, 'kind': 'timer'} for name in handlers}
    env = {'AX_CONVERSATION_HISTORY_DIR': str(history), 'AX_EMBEDDING_PROVIDER': 'provider',
           'AX_EMBEDDING_MODEL': 'model', 'AX_EMBEDDING_URL': 'https://unregistered.invalid/v1/embeddings',
           'AX_EMBEDDING_TOKEN_FILE': str(tmp_path / 'token'), 'AX_EMBEDDING_DIMENSIONS': '1',
           'AX_REDACT_INDEX': str(tmp_path / 'index'), 'AX_PII_LIST': str(tmp_path / 'pii')}
    with TestClient(core) as api:
        def transport(method, url, headers, body, timeout):
            response = api.request(method, url, headers=headers, content=body)
            return response.status_code, dict(response.headers), response.content
        factory = ConversationFactory(tmp_path, index_module.ConversationIndex, environ=env, transport=transport)
        timer = TimerRunner('timer-runner', store, Sink(), handlers, manifests, lambda *args: None,
                            extra_clients={'conversation-index': {'conversation_index': factory}})
        timer.manifests['conversation-index']['schedule'] = {'kind': 'interval', 'every_s': 3600}
        timer.manifests['other']['schedule'] = {'kind': 'interval', 'every_s': 3600}
        original_read_bytes = Path.read_bytes
        def guarded_read_bytes(path):
            if path == history / 'session.jsonl':
                raise AssertionError('disabled index read history')
            return original_read_bytes(path)
        monkeypatch.setattr(Path, 'read_bytes', guarded_read_bytes)
        async def disabled_runs():
            await timer.tick()
            await timer.drain()
            manual = await timer.run_job('conversation-index', '2026-11-08T00:00:00Z')
            return manual
        manual = asyncio.run(disabled_runs())
        assert store.get_run(manual)['reason'] == 'disabled'
        assert sum(row['reason'] == 'disabled' for row in store.runs()) == 2
        assert executed == ['other'] and not network
        for value in ('true', 1, None):
            timer.set_effective('conversation-index', {'mode': 'run', 'revision': 0, 'knobs': {'enabled': value}})
            result = asyncio.run(timer.run_job('conversation-index', '2026-11-08T00:00:00Z'))
            assert store.get_run(result)['reason'] == 'disabled'
        assert not network
        monkeypatch.setattr(Path, 'read_bytes', original_read_bytes)
        executed.clear()
        timer.set_effective('conversation-index', {'mode': 'run', 'revision': 1, 'knobs': {'enabled': True}})
        async def exercise():
            env.pop('AX_EMBEDDING_MODEL')
            missing = await timer.run_job('conversation-index', '2026-11-08T00:00:00Z')
            other_one = await timer.run_job('other', '2026-11-08T00:00:00Z')
            env['AX_EMBEDDING_MODEL'] = 'model'
            unregistered = await timer.run_job('conversation-index', '2026-11-08T00:01:00Z')
            other_two = await timer.run_job('other', '2026-11-08T00:01:00Z')
            return missing, unregistered, other_one, other_two
        missing, unregistered, other_one, other_two = asyncio.run(exercise())
        assert [store.get_run(x)['state'] for x in (missing, unregistered)] == ['failed', 'failed']
        assert [store.get_run(x)['state'] for x in (other_one, other_two)] == ['completed', 'completed']
        assert executed == ['other', 'other'] and not network
        env['AX_EMBEDDING_URL'] = 'https://127.0.0.1/v1/embeddings'
        registered = asyncio.run(timer.run_job('conversation-index', '2026-11-08T00:02:00Z'))
        assert store.get_run(registered)['state'] == 'completed'
        assert len(network) == 1 and network[0][1]['input'] == 'hello'
    store.close()


def test_index_entry_sends_only_inspected_text_and_records_missing_destinations(tmp_path, monkeypatch):
    from axcommon.embedding import OpenAIEmbedding
    import httpx

    sent = []
    endpoint = 'http://fake.invalid/v1/embeddings'
    class Resources:
        def registered_model(self, provider, model):
            return (provider, model) == ('provider', 'model')
        def registered_embedding_endpoint(self, provider, model, url):
            return self.registered_model(provider, model) and url == endpoint
    class Source:
        def __init__(self, text):
            self.text = text
        def changes(self, cursor):
            return iter([{'id': 'a', 'version': '1', 'cursor': 1, 'text': self.text}])
    def fake_post(url, **kwargs):
        assert url == endpoint
        sent.append(kwargs['json']['input'])
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1.0]}]},
                              request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', fake_post)
    for name, contents in [('index', 'salt\n'), ('pii', 'private person\n'), ('token', 'test-token')]:
        (tmp_path / name).write_text(contents)
    (tmp_path / 'redact.yaml').write_text('allow_email_domains: [example.org]\n')
    resources = Resources()
    env = {'AX_CONFIG_DIR': str(tmp_path), 'AX_EMBEDDING_URL': endpoint, 'AX_EMBEDDING_TOKEN_FILE': str(tmp_path / 'token'),
           'AX_EMBEDDING_DIMENSIONS': '1', 'AX_REDACT_INDEX': str(tmp_path / 'index'),
           'AX_PII_LIST': str(tmp_path / 'pii')}
    adapter = OpenAIEmbedding.from_environment(resources, env)
    source = Source('safe text')
    index = plugin('conversation-index').ConversationIndex(tmp_path / 'index.db', source, resources, adapter, adapter.redactor)
    try:
        with pytest.raises(PermissionError):
            invoke('conversation-index', 'conversation_index', (index, 'missing', 'model'))
        assert index.db.execute("SELECT value FROM state WHERE key='index_unavailable'").fetchone() == ('unregistered_embedding_model',)
        for destination in ('', 'http://other.invalid/v1/embeddings'):
            adapter.endpoint = destination
            result = invoke('conversation-index', 'conversation_index', (index, 'provider', 'model'))
            assert result['reason'] == 'unregistered_embedding_endpoint'
            assert index.db.execute("SELECT value FROM state WHERE key='search_unavailable'").fetchone() == ('unregistered_embedding_endpoint',)
        adapter.endpoint = endpoint
        for sensitive in ('sk-' + 'Z' * 25, 'person@example.com', 'private person'):
            source.text = 'safe text ' + sensitive
            assert invoke('conversation-index', 'conversation_index', (index, 'provider', 'model'))['reason'] == 'sensitive_text'
            assert sent == []
        source.text = 'safe text'
        assert invoke('conversation-index', 'conversation_index', (index, 'provider', 'model'))['result'] == 'indexed'
        assert sent == ['safe text']
    finally:
        index.db.close()

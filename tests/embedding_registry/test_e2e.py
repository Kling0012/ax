import json
import _socket
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from starlette.testclient import TestClient

from axcommon.embedding import EmbeddingUnavailable, OpenAIEmbedding
from core.resources.app import Config, build
from core.resources.client import EmbeddingRegistryClient


def test_registry_guards_real_embedding_requests(tmp_path, monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            response = json.dumps({'data': [{'index': 0, 'embedding': [0.5]}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)

    def local_only(sock, address):
        if address != ('127.0.0.1', server.server_port):
            raise RuntimeError('external network forbidden')
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', local_only)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f'http://127.0.0.1:{server.server_port}/v1/embeddings'
    models = tmp_path / 'models.yaml'
    models.write_text(yaml.safe_dump({'models': [{'id': 'provider/model', 'kind': 'model',
        'family': 'provider', 'tier': 'standard', 'status': 'active', 'embedding_endpoint': endpoint}]}))
    app, _, service = build(Config(db=str(tmp_path / 'resources.db'), models_file=str(models)), run_heartbeat_loop=False)
    for name, value in [('index', 'salt\n'), ('pii', 'private person\n'), ('token', 'test-token')]:
        (tmp_path / name).write_text(value)
    env = {'AX_EMBEDDING_URL': endpoint, 'AX_EMBEDDING_TOKEN_FILE': str(tmp_path / 'token'),
           'AX_EMBEDDING_DIMENSIONS': '1', 'AX_REDACT_INDEX': str(tmp_path / 'index'),
           'AX_PII_LIST': str(tmp_path / 'pii')}
    try:
        with TestClient(app) as api:
            def transport(method, url, headers, body, timeout):
                response = api.request(method, url, headers=headers, content=body)
                return response.status_code, dict(response.headers), response.content

            registry = EmbeddingRegistryClient('http://resources', transport=transport)
            adapter = OpenAIEmbedding.from_environment(registry, env)
            with pytest.raises(EmbeddingUnavailable):
                adapter.embed('provider', 'unregistered', 'safe text')
            adapter.endpoint = endpoint.replace('/v1/', '/other/v1/')
            with pytest.raises(EmbeddingUnavailable):
                adapter.embed('provider', 'model', 'safe text')
            assert not requests
            assert registry.registered_embedding_endpoint('provider', 'model', endpoint.upper().replace('/V1/EMBEDDINGS', '/v1/embeddings') + '/')
            assert not registry.registered_embedding_endpoint('provider/', 'model', endpoint)
            assert not registry.registered_embedding_endpoint('provider', 'model', endpoint + '?x=1')
            assert not registry.registered_embedding_endpoint('provider', 'model', endpoint.replace('/v1/', '/V1/'))
            adapter.endpoint = endpoint
            assert adapter.embed('provider', 'model', 'safe text') == [0.5]
            assert requests == [{'model': 'model', 'input': 'safe text'}]
            service.store.close()
            with pytest.raises(EmbeddingUnavailable):
                adapter.embed('provider', 'model', 'safe text')
            assert len(requests) == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

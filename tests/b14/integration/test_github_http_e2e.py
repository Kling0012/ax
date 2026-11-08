import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from plugins.subloop.change.integration.github import GitHub
from plugins.subloop.change.collection.models import ItemKey

HEAD = 'a' * 40
BASE = 'b' * 40
MERGE = 'c' * 40
_NATIVE_CONNECT = socket.socket.connect


@pytest.fixture
def github(monkeypatch):
    state = {'calls': [], 'fail': None, 'merged': False, 'head': HEAD, 'drop': False, 'repo': 'owner/code', 'conflict': False, 'redirect': False, 'issue_body': 'body', 'base': BASE, 'file_metadata': False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def handle_request(self):
            url = urlsplit(self.path)
            path = url.path
            state['calls'].append((self.command, self.path))
            if state['redirect']:
                self.send_response(302); self.send_header('Location', 'http://127.0.0.1:1/steal'); self.end_headers(); return
            if state['fail'] == (path, parse_qs(url.query).get('page', [''])[0]):
                self.send_response(500); self.end_headers(); return
            if self.command == 'GET' and path == '/repos/owner/code/pulls/1' and self.headers.get('Accept') == 'application/vnd.github.v3.diff':
                data = b'diff --git a/src/a b/src/a\n+test\n'
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return
            if self.command == 'PUT':
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if state['conflict'] or data['sha'] != state['head']:
                    self.send_response(409); self.end_headers(); return
                state['merged'] = True
                if state['drop']:
                    self.connection.shutdown(2); self.connection.close(); return
                body = {'merged': True, 'sha': MERGE}
            elif path.endswith('/pulls/1'):
                body = {'number': 1, 'head': {'sha': state['head']}, 'base': {'sha': BASE, 'repo': {'full_name': state['repo']}}, 'state': 'closed' if state['merged'] else 'open', 'merged': state['merged'], 'merged_at': '2026-11-08T00:00:00Z' if state['merged'] else None, 'merge_commit_sha': MERGE if state['merged'] else None, 'mergeable': True, 'draft': False, 'changed_files': 101}
            elif path == '/repos/owner/code/issues/7':
                body = {'number': 7, 'updated_at': 'revision', 'title': 'Issue', 'body': state['issue_body']}
            elif path == '/repos/owner/code/git/ref/heads/main':
                body = {'object': {'sha': state['base']}}
            elif path == '/repos/owner/code':
                body = {'full_name': 'owner/code', 'permissions': {'push': True}}
            else:
                page = int(parse_qs(url.query).get('page', ['1'])[0])
                count = 100 if page == 1 else 1 if page == 2 else 0
                if path.endswith('/pulls/1/files'):
                    body = [{'filename': f'src/{page}-{i}', **({'status': 'modified', 'patch': '@@ text'} if state['file_metadata'] else {})} for i in range(count)]
                    if state['file_metadata'] and page == 2:
                        body = [{'filename': 'src/renamed', 'status': 'renamed', 'previous_filename': 'src/old'}]
                elif path.endswith('/check-runs'):
                    body = {'check_runs': [{'name': f'check-{page}-{i}', 'head_sha': HEAD, 'status': 'completed', 'conclusion': 'success', 'completed_at': '2026-11-08'} for i in range(count)]}
                elif path.endswith('/statuses'):
                    body = [{'context': f'status-{page}-{i}', 'state': 'success', 'updated_at': '2026-11-08'} for i in range(count)]
                elif path.endswith('/comments'):
                    body = [{'id': page * 1000 + i, 'body': 'comment'} for i in range(count)]
                elif path.endswith('/issues'):
                    body = [{'number': page * 1000 + i, 'updated_at': 'revision', 'state': 'open'} for i in range(count)]
                elif path.endswith('/pulls'):
                    body = [{'number': page * 1000 + i, 'updated_at': 'revision', 'state': 'open'} for i in range(count)]
                else:
                    self.send_response(404); self.end_headers(); return
            data = json.dumps(body).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        do_GET = handle_request
        do_PUT = handle_request
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def local_connect(sock, address):
        if address != ('127.0.0.1', server.server_port):
            raise RuntimeError('external connections forbidden')
        return _NATIVE_CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    registry = SimpleNamespace(targets={'owner/code': SimpleNamespace(implementation_repo='owner/code', ci_sources=['check-2-0', 'status-2-0'], project='example', base_ref='main')})
    client = GitHub(registry, 'test-only', base_url=f'http://127.0.0.1:{server.server_port}')
    yield client, state
    server.shutdown(); thread.join(); server.server_close()


def test_all_pages_and_view(github):
    client, state = github
    assert len(client.list_items('owner/code', state='all')) == 202
    assert len(client.list_comments(ItemKey('github', 'owner/code', 'issue', 1))) == 101
    result = client.view({'implementation_repo': 'owner/code', 'issue_repo': 'owner/code', 'pr': 1}, {'implementation_repo': 'owner/code', 'issue_repo': 'owner/code'})
    assert len(result['changed_paths']) == len(result['comments']) == len(result['review_comments']) == 101
    assert [x['status'] for x in result['ci']] == ['success', 'success']
    assert 'requirements' not in result
    assert result['diff'].startswith('diff --git')
    state['fail'] = ('/repos/owner/code/pulls/1/files', '2')
    with pytest.raises(Exception):
        client.view({'implementation_repo': 'owner/code', 'issue_repo': 'owner/code', 'pr': 1}, {'implementation_repo': 'owner/code', 'issue_repo': 'owner/code'})


def test_merge_reconcile_and_identity(github):
    client, state = github
    with pytest.raises(ValueError):
        client.merge('owner/code', 1, sha=BASE, method='merge')
    assert not any(x[0] == 'PUT' for x in state['calls'])
    state['drop'] = True
    with pytest.raises(Exception):
        client.merge('owner/code', 1, sha=HEAD, method='merge')
    assert client.reconcile_merge('owner/code', 1, sha=HEAD)['merge_commit_sha'] == MERGE
    assert client.reconcile_merge('owner/code', 1, sha=BASE)['state'] == 'lost'
    state['repo'] = 'other/repo'
    with pytest.raises(ValueError):
        client.reconcile_merge('owner/code', 1, sha=HEAD)
    with pytest.raises(ValueError):
        client.list_items('other/repo', state='all')
    assert len([x for x in state['calls'] if x[0] == 'PUT']) == 1


def test_merge_conflict_is_not_success(github):
    client, state = github
    state['conflict'] = True
    with pytest.raises(Exception):
        client.merge('owner/code', 1, sha=HEAD, method='merge')
    assert not state['merged']
    assert client.reconcile_merge('owner/code', 1, sha=HEAD)['state'] == 'lost'
    assert len([x for x in state['calls'] if x[0] == 'PUT']) == 1


@pytest.mark.parametrize('path', [
    '/repos/owner/code/issues', '/repos/owner/code/pulls',
    '/repos/owner/code/issues/1/comments', '/repos/owner/code/pulls/1/comments',
    '/repos/owner/code/pulls/1/files', '/repos/owner/code/commits/' + HEAD + '/statuses',
    '/repos/owner/code/commits/' + HEAD + '/check-runs',
])
def test_second_page_failure_is_reported(github, path):
    client, state = github
    state['fail'] = (path, '2')
    with pytest.raises(Exception):
        if path.endswith('/issues') or path.endswith('/pulls'):
            client.list_items('owner/code', state='all')
        else:
            client.view({'implementation_repo': 'owner/code', 'issue_repo': 'owner/code', 'pr': 1}, {'implementation_repo': 'owner/code', 'issue_repo': 'owner/code'})


def test_redirect_does_not_send_token(github):
    client, state = github
    state['redirect'] = True
    with pytest.raises(Exception):
        client.list_items('owner/code', state='all')
    assert len(state['calls']) == 1


def test_issue_snapshot_binds_complete_input_and_registered_base(github):
    client, state = github
    item = {'implementation_repo': 'owner/code', 'issue_repo': 'owner/code',
            'kind': 'issue', 'number': 7, 'pr': None, 'project': 'example'}
    target = {'implementation_repo': 'owner/code', 'issue_repo': 'owner/code'}
    first = client.view(item, target)
    assert first['snapshot_type'] == 'issue' and first['pr'] is None and first['diff'] == ''
    assert first['head_sha'] == first['base_sha'] == BASE
    assert first['source_issue_number'] == 7 and len(first['comments']) == 101
    state['issue_body'] = 'updated'
    assert client.view(item, target)['input_revision'] != first['input_revision']
    state['base'] = HEAD
    assert client.view(item, target)['input_revision'] != first['input_revision']
    state['fail'] = ('/repos/owner/code/issues/7/comments', '2')
    with pytest.raises(Exception):
        client.view(item, target)
    with pytest.raises(ValueError):
        client.view({**item, 'project': 'unregistered'}, target)


def test_files_metadata_preserves_unknown_binary_and_rename(github):
    client, state = github
    state['file_metadata'] = True
    item = {'implementation_repo': 'owner/code', 'issue_repo': 'owner/code', 'pr': 1}
    target = {'implementation_repo': 'owner/code', 'issue_repo': 'owner/code'}
    result = client.view(item, target)
    assert result['files_complete'] is True and len(result['changed_files']) == 101
    assert result['changed_files'][0]['binary'] is False
    assert result['changed_files'][-1] == {'path': 'src/renamed', 'status': 'renamed',
                                          'old_path': 'src/old', 'binary': True}
    state['file_metadata'] = False
    assert client.view(item, target)['files_complete'] is False

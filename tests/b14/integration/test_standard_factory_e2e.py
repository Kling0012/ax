import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plugins.subloop.change.integration.runtime import build_executor
from runtime.subloop.tools import GitHubClient
from axcommon.client import WorkerPoolClient
from tests.fakes.b7 import make_env

_REAL_CONNECT = socket.socket.connect
HEAD = 'a' * 40
BASE = 'b' * 40
MERGE = 'c' * 40


@pytest.fixture
def server(monkeypatch):
    state = {'calls': [], 'receipt': None, 'merged': False, 'pr_body': '', 'pr_title': 'old', 'drop_patch': False, 'created': None, 'fail_pulls': False, 'job_state': 'running', 'drop_job': False, 'issue_mode': False, 'issue_body': 'issue'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            path = self.path.split('?')[0]
            state['calls'].append(('GET', path))
            if path == '/repos/owner/code/pulls/1' and self.headers.get('Accept') == 'application/vnd.github.v3.diff':
                data = b'diff --git a/src/a b/src/a\n+test\n'
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return
            if state['fail_pulls'] and path == '/repos/owner/code/pulls' and 'page=2' in self.path:
                self.send_response(500); self.end_headers(); return
            if path == '/v1/jobs/job-1':
                value = {'id': 'job-1', 'state': state['job_state'], 'report': {'status': 'ready', 'text': json.dumps({'status': 'passed', 'answer': 'ok'})}}
            elif path == '/repos/owner/code/issues':
                value = [{'number': 7, 'updated_at': 'v1', 'state': 'open', 'body': 'issue'}] if state['issue_mode'] else []
            elif path == '/repos/owner/code/pulls':
                value = [] if state['issue_mode'] else [{'number': 1, 'updated_at': 'v1', 'state': 'open', 'body': state['pr_body']}]
                if state['created']:
                    value.append({'number': 2, 'updated_at': 'v1', 'state': 'open', 'body': state['created']['body']})
            elif path == '/repos/owner/code/issues/1':
                value = {'number': 1, 'user': {'login': 'trusted'}}
            elif path == '/repos/owner/code/issues/7':
                value = {'number': 7, 'updated_at': 'v1', 'title': 'Issue', 'body': state['issue_body'], 'user': {'login': 'trusted'}}
            elif path == '/repos/owner/code/git/ref/heads/main':
                value = {'object': {'sha': BASE}}
            elif path == '/repos/owner/code/git/ref/heads/feature':
                value = {'object': {'sha': HEAD}}
            elif path == '/repos/owner/code/pulls/2' and state['created']:
                value = {'number': 2, 'base': {'repo': {'full_name': 'owner/code'}, 'ref': 'main'},
                         'head': {'sha': HEAD, 'ref': 'feature'}, **state['created']}
            elif path == '/repos/owner/code/pulls/1':
                value = {'number': 1, 'base': {'sha': BASE, 'repo': {'full_name': 'owner/code'}},
                         'head': {'sha': HEAD}, 'state': 'closed' if state['merged'] else 'open',
                         'merged': state['merged'], 'merged_at': '2026-11-08T00:00:00Z' if state['merged'] else None,
                         'merge_commit_sha': MERGE if state['merged'] else None, 'changed_files': 1,
                         'mergeable': True, 'draft': False, 'body': state['pr_body'], 'title': state['pr_title']}
            elif path == '/repos/owner/code':
                value = {'full_name': 'owner/code', 'permissions': {'push': True}}
            elif path.endswith('/files'):
                value = [{'filename': 'src/code.py'}]
            elif path.endswith('/check-runs'):
                value = {'check_runs': []}
            else:
                value = []
            self._reply(value)
        def do_PATCH(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(('PATCH', self.path, body))
            state['pr_body'], state['pr_title'] = body['body'], body['title']
            if state['drop_patch']:
                self.connection.shutdown(2); self.connection.close(); return
            self._reply({'ok': True})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(('POST', self.path, body))
            if self.path == '/v1/jobs':
                if state['drop_job']:
                    self.connection.shutdown(2); self.connection.close(); return
                self._reply({'job_id': 'job-1', 'state': 'queued'})
            elif self.path == '/repos/owner/code/pulls':
                state['created'] = {'title': body['title'], 'body': body['body']}
                self._reply({'number': 2})
            elif self.path == '/release/deliveries':
                payload = body['payload']
                state['receipt'] = {'delivery_id': body['delivery_id'], 'destination': 'release',
                    'status': 'delivered', 'delivered_at': 1, 'repo': payload['repo'], 'pr': payload['pr'],
                    'merge_sha': payload['merge_sha'], 'merged_at': payload['merged_at']}
                self._reply(state['receipt'])
            elif self.path == '/v1/notify/deliveries':
                self._reply({'delivery_id': body['delivery_id'], 'destination': 'user', 'status': 'unknown', 'provider_accepted': True})
            elif self.path == '/v1/notify/deliveries/lookup':
                self._reply({'delivery_id': body['delivery_id'], 'destination': 'user', 'status': 'unknown', 'provider_accepted': True})
            elif self.path == '/release/deliveries/lookup':
                self._reply(state['receipt'] or {'status': 'unknown'})
            else:
                self.send_response(404); self.end_headers()
        def _reply(self, value):
            # The test server obeys the page parameter just as GitHub does.
            if 'page=2' in self.path and isinstance(value, list): value = []
            if 'page=2' in self.path and isinstance(value, dict) and 'check_runs' in value: value = {'check_runs': []}
            data = json.dumps(value).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
    def connect(sock, address):
        if address != ('127.0.0.1', srv.server_port): raise RuntimeError('external network forbidden')
        return _REAL_CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    yield f'http://127.0.0.1:{srv.server_port}', state
    srv.shutdown(); thread.join(); srv.server_close()


@pytest.fixture
def runtime(tmp_path, repo_root, fake_clock, monkeypatch, server):
    url, state = server
    target = {'issue_repo': 'owner/code', 'implementation_repo': 'owner/code', 'merge_method': 'merge',
              'ci_sources': [], 'test_recipe': 'fake', 'priority_labels': [], 'execution_cap': 1,
              'allowed_paths': ['**'], 'untrusted_forbidden_paths': ['.github/**', 'package.json', 'Dockerfile*'],
              'trusted_authors': ['trusted'], 'outbound_allowlist': []}
    targets = tmp_path / 'targets.yaml'; targets.write_text(json.dumps({'targets': [target]}))
    monkeypatch.setenv('AX_CHANGE_TARGETS_FILE', str(targets))
    monkeypatch.setenv('AX_CHANGE_RELEASE_URL', url)
    monkeypatch.setenv('AX_CHANGE_HQ_URL', url)
    (tmp_path / 'b7').mkdir()
    env = make_env(tmp_path / 'b7', repo_root, fake_clock)
    env.svc.plugin_dir = repo_root / 'plugins/subloop/change'
    env.svc.github = GitHubClient('fake-github', base_url=url)
    env.svc.pool = WorkerPoolClient(url, 'fake-pool')
    env.svc.ledger = type('Ledger', (), {'_token': 'fake-plugin'})()
    ex, flow, errors = build_executor(root=repo_root, services=env.svc, environ={
        'AX_DATA_DIR': str(tmp_path / 'data'), 'AX_CHANGE_TARGETS_FILE': str(targets),
        'AX_CHANGE_PORT_FACTORY': 'plugins.subloop.change.integration.factory:create_ports'})
    assert not errors
    yield ex, flow, state
    ex.store.close(); ex.change_journal.close()


def execute(ex):
    async def run():
        await ex.run_once()
        while ex._tasks:
            tasks = [task for task in ex._tasks.values() if not task.done()]
            if not tasks: break
            await asyncio.gather(*tasks)
    asyncio.run(run())


def test_factory_graph_collect_only_persists_and_never_writes(runtime):
    ex, flow, state = runtime
    execute(ex)
    rows = flow.ports.collection.store.rows()
    assert len(rows) == 1 and rows[0]['state'] == 'planned'
    assert not any(call[0] != 'GET' for call in state['calls'])
    assert flow.ports.collection.store.pending_operations() == []


def test_factory_parks_when_safety_isolation_unverified(runtime):
    from plugins.subloop.change.safety_pool import SafetyPool
    ex, flow, state = runtime
    assert isinstance(flow.ports.safety, SafetyPool)
    ex.apply('run', {'dry_run': False}, 1)
    execute(ex)
    rows = flow.ports.collection.store.rows()
    assert len(rows) == 1 and rows[0]['hold'] == 'parked'
    assert not any(call[0] != 'GET' for call in state['calls'])


def test_release_receipt_uses_verified_merge_and_issue_repo(runtime):
    ex, flow, state = runtime
    state['merged'] = True
    body = {'repo': 'owner/code', 'pr': 1, 'head_sha': HEAD, 'merge_commit_sha': MERGE,
            'issue': {'provider': 'github', 'issue_repo': 'owner/code', 'kind': 'pr', 'number': 1}}
    assert flow.journal.claim('delivery-1', 'delivery', {'destination': 'release', 'payload': body})
    result = flow.ports.release.send('delivery-1', 'release', body)
    assert result['status'] == 'delivered'
    request = [c[2] for c in state['calls'] if c[:2] == ('POST', '/release/deliveries')][0]
    assert request['payload']['changed_paths'] == ['src/code.py']
    assert request['payload']['issues'] == [{'number': 1, 'issue_repo': 'owner/code'}]
    assert request['payload']['merged_at'] == '2026-11-08T00:00:00Z'


def test_pr_delivery_lost_response_reconciles_without_second_write(runtime):
    ex, flow, state = runtime
    body = {'repo': 'owner/code', 'pr': 1, 'head_sha': HEAD, 'title': 'Ready', 'body': 'Safe text'}
    state['drop_patch'] = True
    first = flow.publications.deliver('pr-delivery', 'github', body)
    assert first['status'] == 'unknown'
    result = flow.ports.publications.reconcile('pr-delivery', 'github')
    assert result['status'] == 'delivered' and result['pr'] == 1
    assert len([call for call in state['calls'] if call[0] == 'PATCH']) == 1
    assert flow.publications.deliver('pr-delivery', 'github', {**body, 'title': 'changed'})['reason'] == 'delivery_identity_mismatch'


def test_release_wrong_merge_is_not_sent(runtime):
    ex, flow, state = runtime
    state['merged'] = True
    body = {'repo': 'owner/code', 'pr': 1, 'head_sha': HEAD, 'merge_commit_sha': BASE,
            'issue': {'provider': 'github', 'issue_repo': 'owner/code', 'kind': 'pr', 'number': 1}}
    assert flow.journal.claim('delivery-2', 'delivery', {'destination': 'release', 'payload': body})
    assert flow.ports.release.send('delivery-2', 'release', body)['status'] == 'unknown'
    assert not any(call[0] == 'POST' and call[1] == '/release/deliveries' for call in state['calls'])


def test_pr_creation_uses_expected_head_and_marker(runtime):
    ex, flow, state = runtime
    body = {'repo': 'owner/code', 'pr': None, 'head_sha': HEAD, 'head': 'feature', 'base': 'main',
            'title': 'Ready', 'body': 'Safe text'}
    result = flow.publications.deliver('new-pr', 'github', body)
    assert result['status'] == 'delivered' and result['pr'] == 2
    assert flow.ports.publications.reconcile('new-pr', 'github')['pr'] == 2
    assert len([c for c in state['calls'] if c[:2] == ('POST', '/repos/owner/code/pulls')]) == 1


def test_notification_provider_receipt_does_not_prove_user_delivery(runtime):
    ex, flow, state = runtime
    body = {'repo': 'owner/code', 'pr': 1, 'head_sha': HEAD, 'instruction_ref': 'card-1'}
    result = flow.notices.deliver('notice-1', 'user', body)
    assert result['status'] == 'unknown'
    assert flow.ports.notifications.reconcile('notice-1', 'user')['status'] == 'unknown'
    writes = [c for c in state['calls'] if c[:2] == ('POST', '/v1/notify/deliveries')]
    assert len(writes) == 1 and writes[0][2]['mode'] == 'run'


def test_failed_collection_never_uses_stale_items(runtime):
    ex, flow, state = runtime
    execute(ex)
    state['fail_pulls'] = True
    ex.apply('run', {'dry_run': False}, 1)
    execute(ex)
    rows = flow.ports.collection.store.rows()
    assert len(rows) == 1 and rows[0]['hold'] == 'parked'
    assert any(event['kind'] == 'change.collection_error' for event in flow.journal.events())
    assert not any(call[0] != 'GET' for call in state['calls'])


def _work_input(flow):
    item = {'provider': 'github', 'issue_repo': 'owner/code', 'kind': 'pr', 'number': 1,
            'implementation_repo': 'owner/code', 'pr': 1, 'project': 'multi', 'author': 'trusted'}
    revision = 'input-1'
    safety = {'status': 'passed', 'head_sha': HEAD, 'input_revision': revision,
              'reviews': [{'reviewer': x, 'verdict': 'safe', 'summary': 'safe', 'concerns': []} for x in ('a', 'b')],
              'worker_input': {'summaries': ['one', 'two']}}
    from plugins.subloop.change.integration.flow import item_key
    flow.journal.put('pipeline', item_key(item), {'safety': safety, 'snapshot': {'head_sha': HEAD, 'input_revision': revision}})
    target = flow.targets['owner/code']
    return {**item, 'worker_input': safety['worker_input'], 'model_budget': 2}, target, safety


def test_work_submission_is_reconciled_without_repeat(runtime):
    from plugins.subloop.change.integration.ports import DependencyUnavailable
    ex, flow, state = runtime
    item, target, safety = _work_input(flow)
    with pytest.raises(DependencyUnavailable): flow.ports.work.run(item, target, operation='answer')
    state['job_state'] = 'ok'
    assert flow.ports.work.run(item, target, operation='answer')['answer'] == 'ok'
    submissions = [c for c in state['calls'] if c[:2] == ('POST', '/v1/jobs')]
    assert len(submissions) == 1
    from axcommon.schemas import validate
    validate('job-request', submissions[0][2])
    with pytest.raises(DependencyUnavailable): flow.ports.work.plan(item, target, safety=safety)


def test_work_lost_submit_is_not_resent(runtime):
    from plugins.subloop.change.integration.ports import DependencyUnavailable
    ex, flow, state = runtime
    item, target, _ = _work_input(flow)
    state['drop_job'] = True
    for _ in range(2):
        with pytest.raises(DependencyUnavailable): flow.ports.work.run(item, target, operation='answer')
    assert len([c for c in state['calls'] if c[:2] == ('POST', '/v1/jobs')]) == 1


def test_revision_bound_evidence_persists_and_rejects_mismatch(runtime):
    from plugins.subloop.change.integration.evidence_store import EvidenceStore
    from tests.fakes.b14.integration import evidence as verification_evidence
    ex, flow, state = runtime
    item, target, safety = _work_input(flow)
    facts = flow.ports.github.view(item, target)
    safety['input_revision'] = facts['input_revision']
    verification = {'status': 'passed', 'evidence': [verification_evidence()]}
    from plugins.subloop.change.integration.flow import item_key
    pub_id = 'pr:evidence:' + HEAD + ':' + facts['input_revision']
    pub = {'repo': 'owner/code', 'pr': 1, 'head_sha': HEAD, 'body': 'approved', 'source_item': item_key(item)}
    assert flow.journal.claim(pub_id, 'delivery', {'destination': 'github', 'payload': pub})
    flow.journal.finish(pub_id, 'succeeded', {'status': 'delivered', 'delivered_at': 1})
    requirements = [{'id': 'r1', 'source': 'review', 'text': 'do work', 'status': 'done', 'evidence': ['test:ok']}]
    store = EvidenceStore(flow.journal, flow.ports.collection.registry, flow.ports.collection.store)
    store.record_safety(item, facts, safety)
    store.record_verification(item, facts, verification)
    assert flow.journal.get('verification_evidence', store._scope(item, facts))['verification']['status'] == 'passed'
    store.save_reviewed(item, facts, safety=safety, verification=verification, requirements=requirements,
                        positions={}, publication_id=pub_id)
    other = EvidenceStore(flow.journal, flow.ports.collection.registry, flow.ports.collection.store)
    assert other.read(item, target, facts)['required_ids'] == ['r1']
    assert other.read(item, target, {**facts, 'input_revision': 'changed'}) == {}
    assert other.read(item, target, {**facts, 'comments': [{'id': 9, 'updated_at': 'new'}]}) == {}
    with pytest.raises(ValueError):
        other.save_reviewed({**item, 'pr': 2}, facts, safety=safety, verification=verification,
                            requirements=requirements, positions={}, publication_id=pub_id)


def test_evidence_requires_comment_ack_in_collection_store(runtime):
    from plugins.subloop.change.integration.evidence_store import EvidenceStore
    from plugins.subloop.change.collection import ItemKey
    from tests.fakes.b14.integration import evidence as verification_evidence
    ex, flow, state = runtime
    item, target, safety = _work_input(flow)
    facts = flow.ports.github.view(item, target)
    facts['comments'] = [{'id': 9, 'updated_at': 'v1'}]
    safety['input_revision'] = facts['input_revision']
    from plugins.subloop.change.integration.flow import item_key
    pub_id = 'pr:comments:' + HEAD + ':' + facts['input_revision']
    pub = {'repo': 'owner/code', 'pr': 1, 'head_sha': HEAD, 'body': 'approved', 'source_item': item_key(item)}
    assert flow.journal.claim(pub_id, 'delivery', {'destination': 'github', 'payload': pub})
    flow.journal.finish(pub_id, 'succeeded', {'status': 'delivered', 'delivered_at': 1})
    store = EvidenceStore(flow.journal, flow.ports.collection.registry, flow.ports.collection.store)
    arguments = dict(safety=safety, verification={'status': 'passed', 'evidence': [verification_evidence()]},
                     requirements=[{'id': 'r1', 'source': 'review', 'text': 'work', 'status': 'done', 'evidence': ['test:ok']}],
                     positions={'comments:9': 'v1'}, publication_id=pub_id)
    with pytest.raises(ValueError): store.save_reviewed(item, facts, **arguments)
    key = ItemKey('github', 'owner/code', 'pr', 1)
    flow.ports.collection.store.acknowledge_comments(key, [{'id': 9, 'revision': 'v1'}])
    store.save_reviewed(item, facts, **arguments)
    assert store.read(item, target, facts)['unprocessed_comments'] == []
    flow.ports.collection.store.acknowledge_comments(key, [{'id': 9, 'revision': 'v2'}])
    assert store.read(item, target, facts) == {}


def test_registered_tdd_plan_uses_only_target_configuration(runtime):
    from dataclasses import replace
    from plugins.subloop.change.collection import TargetRegistry
    from plugins.subloop.change.integration.work import Work
    ex, flow, _ = runtime
    item, target, safety = _work_input(flow)
    original = flow.ports.collection.registry.targets['owner/code']
    configured = replace(original, base_ref='release/main', tdd_verify='python -m pytest tests/unit')
    registry = TargetRegistry([configured])
    work = Work(ex.svc.pool, flow.journal, registry)
    # Values supplied in an item or target argument cannot replace registry values.
    item.update(branch='attacker', verify='bad', base_ref='attacker')
    target.update(base_ref='attacker', tdd_verify='bad')
    plan = work.plan(item, target, safety=safety)
    assert plan['base_ref'] == 'release/main'
    assert plan['verify'] == 'python -m pytest tests/unit'
    assert plan['branch'].startswith('ax/change-') and plan['branch'] != 'attacker'
    assert any(node.type == 'git.worktree' for node in ex.graph.nodes.values())


@pytest.mark.parametrize('field,value', [('base_ref', '../other'), ('base_ref', '-unsafe'),
                                         ('base_ref', 'x.lock/y'), ('tdd_verify', ''),
                                         ('tdd_verify', 'x\x00y')])
def test_invalid_registered_tdd_configuration_is_rejected(runtime, field, value):
    from dataclasses import replace
    ex, flow, _ = runtime
    original = flow.ports.collection.registry.targets['owner/code']
    with pytest.raises(ValueError):
        replace(original, **{field: value})


def test_graph_prepare_passes_registered_tdd_plan_to_include(runtime):
    from dataclasses import replace
    ex, flow, state = runtime
    registry = flow.ports.collection.registry
    registry.targets['owner/code'] = replace(registry.targets['owner/code'], base_ref='main', tdd_verify='python -m pytest tests/unit')
    flow.targets['owner/code'].update(base_ref='main', tdd_verify='python -m pytest tests/unit')
    builder = flow.ports.collection.item_builder
    flow.ports.collection.item_builder = lambda row, key, target: {**builder(row, key, target), 'route': 'change'}
    class Safe:
        def review(self, item):
            snapshot = item['snapshot']
            return {'status': 'passed', 'head_sha': snapshot['head_sha'], 'input_revision': snapshot['input_revision'],
                    'reviews': [{'reviewer': x, 'verdict': 'safe', 'summary': 'safe', 'concerns': []} for x in ('a', 'b')],
                    'worker_input': {'summaries': ['one', 'two']}}
    flow.ports.safety = Safe()
    ex.apply('run', {'dry_run': False}, 1)
    execute(ex)
    records = [row for row in ex.store.items() if not row['item_id'].startswith('run:')]
    assert len(records) == 1
    data = ex.store.get_item(records[0]['item_id'])['data']
    assert data['base_ref'] == 'main' and data['verify'] == 'python -m pytest tests/unit'
    assert data['branch'].startswith('ax/change-')
    saved = flow.journal.get('pipeline', records[0]['item_id'])
    assert saved and flow.ports.github.evidence_store.journal.get('review_evidence',
        flow.ports.github.evidence_store._scope(saved['item'], saved['snapshot']))['safety']['status'] == 'passed'
    assert not any(c[0] == 'POST' and c[1] == '/v1/jobs' for c in state['calls'])


def test_graph_rechecks_pending_safety_and_parks_failure(runtime, fake_clock):
    ex, flow, state = runtime
    class PendingThenFailed:
        calls = 0
        def review(self, item):
            self.calls += 1
            return {'status': 'pending' if self.calls == 1 else 'failed', 'reason': 'safety_pending' if self.calls == 1 else 'safety_unavailable'}
    safety = PendingThenFailed()
    flow.ports.safety = safety
    ex.apply('run', {'dry_run': False}, 1)
    execute(ex)
    rows = flow.ports.collection.store.rows()
    assert len(rows) == 1 and rows[0]['hold'] == 'retry' and rows[0]['reason'] == 'safety_pending'
    assert safety.calls == 1
    fake_clock.advance(61)
    execute(ex)
    rows = flow.ports.collection.store.rows()
    assert safety.calls == 2
    assert rows[0]['hold'] == 'parked' and rows[0]['reason'] == 'safety_unavailable'
    assert not any(c[0] in {'POST', 'PATCH', 'PUT'} for c in state['calls'])


@pytest.mark.parametrize('stale', [False, True])
def test_issue_graph_accepts_reviewed_work_with_registered_base(runtime, stale):
    from dataclasses import replace
    ex, flow, state = runtime
    state['issue_mode'] = True
    state['job_state'] = 'failed'
    ex.svc.repos['example'] = 'https://example.invalid/owner/code.git'
    ex.svc.git.head_sha = BASE
    if stale:
        git_run = ex.svc.git.run
        async def mutate_after_worktree(args, **kwargs):
            result = await git_run(args, **kwargs)
            if args[0] == 'worktree':
                state['issue_body'] = 'changed after review'
            return result
        ex.svc.git.run = mutate_after_worktree
    registry = flow.ports.collection.registry
    registry.targets['owner/code'] = replace(registry.targets['owner/code'], project='example', base_ref='main', tdd_verify='verify')
    flow.targets['owner/code'].update(project='example', base_ref='main', tdd_verify='verify')
    class Safety:
        def review(self, item):
            snap = item['snapshot']
            assert snap['snapshot_type'] == 'issue' and snap['pr'] is None
            assert snap['head_sha'] == snap['base_sha'] == BASE
            return {'status': 'passed', 'head_sha': snap['head_sha'], 'input_revision': snap['input_revision'],
                    'reviews': [{'reviewer': x, 'verdict': 'safe', 'summary': 'safe', 'concerns': []} for x in ('a', 'b')],
                    'worker_input': {'summaries': ['one', 'two']}}
    flow.ports.safety = Safety()
    ex.apply('run', {'dry_run': False}, 1)
    execute(ex)
    records = [row for row in ex.store.items() if not row['item_id'].startswith('run:')]
    assert len(records) == 1
    data = ex.store.get_item(records[0]['item_id'])['data']
    assert data['project'] == 'example' and data['base_ref'] == BASE and data['verify'] == 'verify'
    assert data['pr'] is None and data['body'] == 'one\ntwo'
    submissions = [c for c in state['calls'] if c[:2] == ('POST', '/v1/jobs')]
    if stale:
        assert not submissions
        assert flow.ports.collection.store.rows()[0]['reason'] == 'reviewed_input_changed'
    else:
        assert len(submissions) == 1 and submissions[0][2]['role'] == 'test_design'
        assert submissions[0][2]['project'] == 'example'
    saved = flow.journal.get('pipeline', records[0]['item_id'])
    assert saved['snapshot']['snapshot_type'] == 'issue'
    scope = flow.ports.github.evidence_store._scope(saved['item'], saved['snapshot'])
    assert flow.journal.get('review_evidence', scope)['safety']['status'] == 'passed'
    with pytest.raises(ValueError, match='pr_evidence_required'):
        flow.ports.github.evidence_store.record_verification(saved['item'], saved['snapshot'], {})

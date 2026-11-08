from io import StringIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

from starlette.applications import Starlette

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "host/lib"))
import ax_cli
from core.intake.dest.b12.read_relay import register
from core.intake.vias import Ident
from tests.fakes.b12.cli.entry import FakeCore, request


def test_cli_start_reads_and_denial():
    app = Starlette()
    app.state.intake = SimpleNamespace(transport=FakeCore(), audit=None)
    register(app)
    identity = Ident('local', 'commander', 'example', 'operate')

    def invoke(argv, ident=identity):
        out = StringIO()
        def transport(method, path, body):
            parsed = urlsplit(path)
            return request(app, parsed.path, ident=ident, method=method, query=parsed.query)
        code = ax_cli.main(argv, transport=transport, environ={}, stdout=out, stderr=StringIO())
        return code, json.loads(out.getvalue())

    code, body = invoke(['whoami'])
    assert code == 0 and body['data']['seat_id'] == 'example'
    code, body = invoke(['guide', '--version'])
    assert code == 0 and body['data']['guide_version'] and 'guide' not in body['data']
    code, body = invoke(['guide'])
    assert code == 0 and body['data']['guide']
    code, body = invoke(['whoami'], Ident('hq', 'hq', 'other', 'irreversible'))
    assert code == 6 and body['error']['code'] == 'receptacle_not_allowed'
    code, body = invoke(['brief', '--new-session'])
    assert code == 0 and body['data']['session']
    assert all(section['status'] == 'unavailable' for section in body['data']['sections'])


def test_cli_destinations_delegates_to_existing_endpoint_and_rejects_other_via():
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    app = Starlette()
    app.state.intake = SimpleNamespace(transport=FakeCore(), audit=None)
    seen = []

    async def destinations(request):
        seen.append(request.scope['ax_ident'].principal)
        return JSONResponse({'destinations': [{'name': 'example'}], 'reads': []})

    app.router.routes.append(Route('/v1/destinations', destinations, methods=['GET']))
    register(app)

    def invoke(ident):
        out = StringIO()
        def transport(method, path, body):
            parsed = urlsplit(path)
            return request(app, parsed.path, ident=ident, method=method, query=parsed.query)
        code = ax_cli.main(['destinations'], transport=transport, environ={}, stdout=out, stderr=StringIO())
        return code, json.loads(out.getvalue())

    code, body = invoke(Ident('local', 'commander', 'example', 'operate'))
    assert code == 0 and body['data']['destinations'] == [{'name': 'example'}]
    code, body = invoke(Ident('hq', 'hq', 'other', 'irreversible'))
    assert code == 6 and body['error']['code'] == 'receptacle_not_allowed'
    assert seen == ['example']


def test_results_cli_checks_owner_and_project_before_pool_lookup():
    from threading import RLock
    from core.intake.dest.b12.common.store import Store

    app = Starlette()
    app.state.intake = SimpleNamespace(transport=FakeCore(), audit=None)
    store = Store()
    store.put('read:request', {'job_id': 'job-1', 'owner': 'alice',
                               'payload': {'project': 'p', 'role': 'research', 'title': 'read'}})
    calls = []
    def get(path):
        calls.append(path)
        return {'id': 'job-1', 'state': 'ok', 'report': {'text': 'result'}}
    app.state.b12 = SimpleNamespace(lock=RLock(), db=store, pool=SimpleNamespace(get=get))
    register(app)

    def invoke(project, principal='alice', job='job-1'):
        out = StringIO()
        def transport(method, path, body):
            parsed = urlsplit(path)
            return request(app, parsed.path, ident=Ident('local', 'commander', principal, 'operate'),
                           method=method, query=parsed.query)
        code = ax_cli.main(['results', '--project', project, '--job-id', job], transport=transport,
                           environ={}, stdout=out, stderr=StringIO())
        return code, json.loads(out.getvalue())

    code, body = invoke('p')
    assert code == 0 and body['data']['report']['text'] == 'result'
    assert calls == ['/v1/jobs/job-1']
    assert invoke('other')[0] != 0
    assert invoke('p', principal='bob')[0] != 0
    assert invoke('p', job='missing')[0] != 0
    assert calls == ['/v1/jobs/job-1']



def test_brief_public_cli_scopes_sources_and_survives_failures(monkeypatch):
    app = Starlette()
    app.state.intake = SimpleNamespace(transport=FakeCore(), audit=None)
    register(app)
    calls = []
    failed = set()
    waiting_count = [7]
    async def get(component, path, params=None):
        calls.append((component, path, params))
        if path in failed:
            raise ConnectionError('offline')
        if path == '/v1/todos':
            rows = [{'id': 'todo-' + str(i), 'state': 'waiting_user', 'project': 'p',
                     'title': 'private title', 'updated_at': '2026-01-01T00:00:00Z'} for i in range(waiting_count[0])]
            return {'todos': rows + [{'id': 'parked', 'state': 'parked', 'project': 'p', 'check_at': '2020-01-01T00:00:00Z'}]}
        if path == '/v1/commands':
            return {'commands': [{'id': 'command', 'state': 'expired_pending'}]}
        if path == '/v1/plugins':
            return {'plugins': []}
        if path == '/v1/projections/jobs':
            return {'rows': [{'key': 'job', 'value': {'state': 'running'}}]}
        if path == '/v1/events':
            return {'events': [{'id': 'ev', 'seq': 1, 'ts': '2026-01-02T00:00:00Z', 'kind': 'task.state', 'project': 'p'}], 'next_after': 1}
        if path == '/v1/promises':
            return {'promises': [{'id': 'promise', 'state': 'open', 'todo_id': 'todo-0', 'due_at': '2020-01-01T00:00:00Z'}]}
        if path == '/v1/goals':
            return {'goals': [{'id': 'goal', 'state': 'active'}]}
        if path == '/v1/decisions':
            return {'decisions': [{'id': 'decision', 'kind': 'handoff', 'status': 'active', 'ts': '2026-01-02T00:00:00Z', 'body': 'private'}]}
        raise AssertionError(path)
    app.state.b12_read_relay._get = get

    def invoke(args, ident=None, model=None):
        out = StringIO()
        async def wrapped(scope, receive, send):
            scope['ax_declared'] = {'model': model or ''}
            await app(scope, receive, send)
        def transport(method, path, body, headers=None):
            parsed = urlsplit(path)
            return request(wrapped, parsed.path, ident=ident or Ident('local', 'commander', 'alice', 'operate'),
                           method=method, query=parsed.query)
        code = ax_cli.main(args, transport=transport, environ={}, stdout=out, stderr=StringIO())
        return code, out.getvalue()

    code, raw = invoke(['brief', '--new-session', '--since', '2026-01-01T00:00:00Z'])
    body = json.loads(raw)['data']
    assert code == 0 and body['session'] and body['redacted']
    sections = body['sections']
    assert [s['name'] for s in sections] == ['summary', 'waiting_user', 'approvals', 'running', 'claims', 'events', 'commitments', 'decisions', 'next']
    assert sections[1]['status'] == 'ok' and len(sections[1]['items']) == 5 and sections[1]['omitted'] == 2
    assert sections[3]['status'] == 'ok' and sections[5]['status'] == 'ok' and sections[7]['status'] == 'ok'
    assert sections[2]['status'] == sections[4]['status'] == 'unavailable'
    assert 'private' not in raw
    assert all(x['kind'] == 'waiting_user' for x in sections[8]['items'])
    waiting_count[0] = 1
    _, action_raw = invoke(['brief'])
    actions = json.loads(action_raw)['data']['sections'][8]['items']
    assert [x['kind'] for x in actions] == ['waiting_user', 'overdue_promise', 'parked', 'expired_pending']
    assert all(path.startswith('/v1/') for _, path, _ in calls)
    failed.add('/v1/todos')
    code, raw = invoke(['brief'])
    sections = json.loads(raw)['data']['sections']
    assert code == 0 and sections[1]['status'] == 'unavailable' and sections[5]['status'] == 'ok'
    failed.clear()
    for path, index in [('/v1/commands', 3), ('/v1/projections/jobs', 3), ('/v1/events', 5),
                        ('/v1/promises', 6), ('/v1/goals', 6), ('/v1/decisions', 7)]:
        failed.add(path)
        _, failure_raw = invoke(['brief'])
        failed_sections = json.loads(failure_raw)['data']['sections']
        assert failed_sections[index]['status'] == 'unavailable'
        assert failed_sections[1]['status'] == 'ok'
        failed.clear()
    calls.clear()
    code, raw = invoke(['brief', '--project', 'p', '--section', 'decisions'])
    assert code == 0 and json.loads(raw)['data']['sections'][0]['name'] == 'decisions'
    assert next(p for _, path, p in calls if path == '/v1/decisions')['project'] == 'p'
    fixed = ['brief', '--short', '--since', '2026-01-01T00:00:00Z']
    _, json_raw = invoke(fixed)
    expected = json.loads(json_raw)['data']
    code, raw = invoke(['--format', 'text', *fixed])
    assert code == 0 and len(raw.splitlines()) <= 25
    lines = raw.splitlines()
    header = json.loads(lines[0].split(' ', 1)[1])
    summary = header.pop('summary')
    assert header == {k: v for k, v in expected.items() if k != 'sections'}
    actual = [summary]
    for line in lines[1:]:
        if line.startswith('ほか '):
            assert actual[-1]['next'] in line
            continue
        if line.startswith('- '):
            actual[-1]['items'].append(json.loads(line[2:]))
        else:
            meta = json.loads(line.split(' ', 1)[1])
            meta['items'] = []
            actual.append(meta)
    actual[0].pop('retrieved_at')
    expected['sections'][0].pop('retrieved_at')
    assert actual == expected['sections']
    code, raw = invoke(['brief'], ident=Ident('hq', 'hq', 'user', 'irreversible'))
    assert code == 6


def test_brief_model_clearance_and_secret_redaction(tmp_path, monkeypatch):
    import core.intake.dest.b12.brief as mod
    (tmp_path / 'commander-models.yaml').write_text('models:\n  - match: "codex/"\n    max_class: sensitive\n')
    (tmp_path / 'models.yaml').write_text('models:\n  - id: codex/ok\n    tier: standard\n    status: active\n')
    monkeypatch.setattr(mod, 'config_dir', lambda: tmp_path)
    app = Starlette()
    app.state.intake = SimpleNamespace(transport=FakeCore(), audit=None)
    register(app)
    async def get(component, path, params=None):
        if path == '/v1/todos':
            return {'todos': [{'id': 'one', 'state': 'waiting_user', 'title': 'Visible', 'reason': 'alice@example.org'}]}
        raise ConnectionError('offline')
    app.state.b12_read_relay._get = get
    def invoke(model):
        out = StringIO()
        async def wrapped(scope, receive, send):
            scope['ax_declared'] = {'model': model}
            await app(scope, receive, send)
        def transport(method, path, body, headers=None):
            parsed = urlsplit(path)
            return request(wrapped, parsed.path, ident=Ident('local', 'commander', 'alice', 'operate'), query=parsed.query)
        code = ax_cli.main(['--model', model, 'brief'], transport=transport, environ={}, stdout=out, stderr=StringIO())
        return code, json.loads(out.getvalue())
    code, body = invoke('codex/ok')
    assert code == 0 and body['data']['redacted'] is False
    assert body['data']['sections'][1]['items'][0]['title'] == 'Visible'
    assert 'alice@example.org' not in json.dumps(body)
    code, body = invoke('codex/unknown')
    assert code == 0 and body['data']['redacted'] is True
    assert 'Visible' not in json.dumps(body)


def test_brief_approval_claim_pagination_and_project_mapping():
    from threading import RLock
    import shlex

    now_future = '2099-01-01T00:00:00Z'
    records = [
        {'approval_id': 'approval-a', 'project': 'p', 'destination': 'example', 'requested_by': 'bob',
         'created_at': '2026-01-01T00:00:00Z', 'expires_at': now_future, 'state': 'pending_user'},
        {'approval_id': 'approval-b', 'project': 'q', 'destination': 'example', 'requested_by': 'bob',
         'created_at': '2026-01-01T00:00:00Z', 'expires_at': now_future, 'state': 'pending_user'},
    ]
    claim_data = {'control:a': {'owner': {'principal': 'alice', 'session': 's', 'via': 'local'},
                                'lease_until': now_future, 'max_until': now_future,
                                'acquired_at': '2026-01-01T00:00:00Z'},
                  'expired': {'owner': {'principal': 'bob'}, 'lease_until': '2020-01-01T00:00:00Z',
                              'max_until': '2020-01-01T00:00:00Z', 'acquired_at': '2020-01-01T00:00:00Z'}}
    claim_file = type('ClaimFile', (dict,), {'broken': False})(claim_data)
    handler = SimpleNamespace(_lock=RLock(), file=claim_file)
    app = Starlette()
    app.state.intake = SimpleNamespace(transport=FakeCore(), audit=None, dest_handlers={'claim.op': handler})
    app.state.approvals = SimpleNamespace(store=SimpleNamespace(list=lambda: records))
    app.state.b12 = SimpleNamespace(lock=RLock(), db=SimpleNamespace(work_requests=lambda: [
        {'job_id': 'job-p', 'payload': {'project': 'p'}}, {'job_id': 'job-q', 'payload': {'project': 'q'}}]))
    register(app)
    async def get(component, path, params=None):
        if path == '/v1/todos':
            return {'todos': [{'id': f'todo-{i}', 'project': 'p', 'state': 'waiting_user', 'goal_ids': ['goal-p']} for i in range(8)]}
        if path == '/v1/commands':
            return {'commands': []}
        if path == '/v1/plugins':
            return {'plugins': [{'mode': 'run'}]}
        if path == '/v1/projections/jobs':
            return {'rows': [{'key': k, 'value': {'state': 'running'}} for k in ('job-p', 'job-q', 'job-unknown')]}
        if path == '/v1/events':
            return {'events': [], 'next_after': 0}
        if path == '/v1/promises':
            return {'promises': []}
        if path == '/v1/goals':
            return {'goals': [{'id': 'goal-p', 'state': 'active'}, {'id': 'unknown', 'state': 'active'}]}
        if path == '/v1/decisions':
            return {'decisions': []}
        raise AssertionError(path)
    app.state.b12_read_relay._get = get

    def invoke(argv, identity=None):
        out = StringIO()
        def transport(method, path, body):
            parsed = urlsplit(path)
            return request(app, parsed.path, ident=identity or Ident('local', 'commander', 'alice', 'operate'),
                           method=method, query=parsed.query)
        code = ax_cli.main(argv, transport=transport, environ={}, stdout=out, stderr=StringIO())
        return code, json.loads(out.getvalue())

    code, body = invoke(['brief', '--since', '2026-01-01T00:00:00Z'])
    assert code == 0
    sections = body['data']['sections']
    assert sections[2]['status'] == 'ok' and len(sections[2]['items']) == 2
    assert sections[4]['status'] == 'ok' and sections[4]['items'][0]['own'] is True
    assert 'expired' in claim_file  # a GET must never expire or rewrite claims
    assert sections[8]['status'] == 'ok'
    next_command = sections[1]['next']
    code, page = invoke(shlex.split(next_command)[1:])
    assert code == 0 and len(page['data']['sections'][0]['items']) == 3
    assert page['data']['sections'][0]['omitted'] == 0
    code, scoped = invoke(['brief', '--project', 'p'])
    scoped_sections = scoped['data']['sections']
    assert [a['approval_id'] for a in scoped_sections[2]['items']] == ['approval-a']
    assert 'job-p' in json.dumps(scoped_sections[3]) and 'job-q' not in json.dumps(scoped_sections[3])
    assert 'job-unknown' not in json.dumps(scoped_sections[3])
    assert scoped_sections[3]['status'] == 'unavailable'
    assert 'goal-p' in json.dumps(scoped_sections[6]) and 'unknown' not in json.dumps(scoped_sections[6]['items'])
    assert scoped_sections[6]['status'] == 'unavailable'
    assert scoped_sections[4]['status'] == 'unavailable'
    claim_file.broken = True
    _, broken = invoke(['brief'])
    assert broken['data']['sections'][4]['status'] == 'unavailable'
    assert broken['data']['sections'][2]['status'] == 'ok'
    claim_file.broken = False
    def unavailable():
        raise OSError('offline')
    app.state.approvals.store.list = unavailable
    _, broken = invoke(['brief'])
    assert broken['data']['sections'][2]['status'] == 'unavailable'
    assert broken['data']['sections'][4]['status'] == 'ok'
    code, _ = invoke(['brief'], Ident('hq', 'hq', 'user', 'irreversible'))
    assert code == 6

"""Exercise the sites snapshot from timer runner through HQ."""
import asyncio
import json

from tests.b16a.test_t62_e2e import DIRECT, setup
from tests.fakes.b10b.runtime import call
from runtime.subloop.store import Store
from runtime.timer_runner.runner import TimerRunner, load_handler


def test_sites_snapshot_timer_to_hq(setup, tmp_path, monkeypatch, repo_root):
    app, source, _, _ = setup
    sites = [dict(id=f'site-{i}', name=f'Site {i}', url=f'https://example.invalid/{i}',
                  exposure='public' if i < 46 else 'tailnet', auth='none',
                  category='service', hidden=False, stale=False) for i in range(61)]
    original = json.dumps({'sites': sites, 'collection_errors': []})
    source.write_text(original)
    root = tmp_path / 'registrations'
    (root / 'plugins/registry.d').mkdir(parents=True)
    (root / 'compose.d').mkdir()
    registry = 'plugins:\n- {id: sample, kind: timer, path: sample.yaml}\n'
    (root / 'plugins/registry.yaml').write_text(registry)
    (root / 'plugins/registry.d/sample.yaml').write_text(registry)
    (root / 'compose.d/sample.yaml').write_text('services:\n  example: {}\n')
    state = tmp_path / 'shared/state.json'
    monkeypatch.setenv('AX_HOME_IN_CONTAINER', str(root))
    monkeypatch.setenv('AX_ENTRY_STATE_FILE', str(state))
    class Sink:
        def emit(self, *args, **kwargs):
            pass
    store = Store(tmp_path / 'timer-sites.sqlite')
    handler = load_handler(repo_root / 'plugins/timer/entry-index')
    runner = TimerRunner('timer-runner', store, Sink(), {'entry-index': handler},
                         {'entry-index': {'id': 'entry-index', 'kind': 'timer'}}, lambda *args: None)
    def run():
        ident = asyncio.run(runner.run_job('entry-index', '2030-01-01T00:00:00Z'))
        return store.get_run(ident)['state']
    assert run() == 'completed'
    first = json.loads(state.read_text())
    assert first['data']['sites'] == sites
    body = call(app, 'GET', '/entry', headers=DIRECT).text
    assert '候補: 61 件' in body and body.count('<li>') == 61
    assert body.count('<strong>公開中</strong>') == 46
    assert 'https://example.invalid/60' in body
    assert source.read_text() == original
    for invalid in ('invalid json', '{"sites": [{"id": "bad"}]}'):
        source.write_text(invalid)
        assert run() == 'failed'
        failed = json.loads(state.read_text())
        assert failed['data'] == first['data'] and failed['last_success_at'] == first['last_success_at']
        body = call(app, 'GET', '/entry', headers=DIRECT).text
        assert '取得に失敗しました' in body and 'Site 60' in body
    monkeypatch.setenv('AX_ENTRY_SITES_FILE', str(tmp_path / 'missing.json'))
    assert run() == 'failed'
    assert json.loads(state.read_text())['data'] == first['data']
    store.close()

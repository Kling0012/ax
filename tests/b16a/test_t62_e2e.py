"""T-62 through the authenticated HQ HTTP interface."""
from __future__ import annotations

import ipaddress as _fixture_ipaddress
_NET_FIXTURE_3258 = str(_fixture_ipaddress.ip_network("100.64.0.0/10")[3258])

import json
from pathlib import Path

import pytest

from core.hq.app import create_app
from core.hq.auth import AccessVerifier, Authenticator
from core.hq.settings import Settings
from tests.fakes.b10b.runtime import JWKS, call, signed


class Ledger:
    def __init__(self):
        self.failed = False

    def read(self, path):
        if self.failed:
            return 503, {"ok": False}
        data = {"state": "focused", "as_of": "2030-01-01T00:00:00Z"} if path == "/v1/focus-state" else {"segments": [{"kind": "work"}]}
        return 200, {"ok": True, "data": data}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    sites = tmp_path / "sites.json"
    sites.write_text(json.dumps({"sites": [{"id": f"site-{n}", "name": f"Site {n}", "url": f"https://example.invalid/{n}", "exposure": "public" if n < 2 else "tailnet"} for n in range(61)]}))
    source = tmp_path / "map.json"
    source.write_text(json.dumps({"maps": [{"id": "map-1", "name": "System map", "boxes": [{"name": "Service"}], "flows": [{"name": "Request", "steps": ["HQ", "Service"]}]}]}))
    state = tmp_path / "entry-state.json"
    state.write_text(json.dumps({"data": json.loads(sites.read_text()), "last_success_at": "2030-01-01T00:00:00Z", "latest": {"status": "success", "at": "2030-01-01T00:00:00Z"}}))
    monkeypatch.setenv("AX_ENTRY_STATE_FILE", str(state))
    monkeypatch.setenv("AX_ENTRY_SITES_FILE", str(sites))
    monkeypatch.setenv("AX_AUTOMATION_MAP_SOURCE", str(source))
    ext = tmp_path / "ext"
    ext.mkdir()
    ext.joinpath("b16a_t62.py").symlink_to(Path(__file__).resolve().parents[2] / "core/hq/ext/b16a_t62.py")
    settings = Settings(public_proxy_ip="192.0.2.1", slots_dir=tmp_path, ui_dir=tmp_path / "no-ui")
    verifier = AccessVerifier("https://access.ci.invalid", lambda: "test-aud", lambda: JWKS, clock=lambda: 1000)
    auth = Authenticator(settings.public_proxy_ip, lambda: {"test-user-3@example.com"}, verifier)
    app = create_app(settings=settings, authenticator=auth, ext_dir=ext, run_heartbeat_loop=False)
    assert app.state.ext_results == {"b16a_t62": None}
    ledger = Ledger()
    app.state.hq_api = ledger
    return app, sites, source, ledger


DIRECT = {"x-forwarded-for": "__NET_FIXTURE_3258__".replace('__NET_FIXTURE_3258__', _NET_FIXTURE_3258), "tailscale-user-login": "test-user-3@example.com"}
PUBLIC = {"x-forwarded-for": "192.0.2.1", "cf-access-jwt-assertion": signed()}


def test_entry_displays_all_candidates_and_public_marks(setup):
    app, sites, _, _ = setup
    response = call(app, "GET", "/entry", headers=DIRECT)
    assert response.status_code == 200
    assert f'候補: {len(json.loads(sites.read_text())["sites"])} 件' in response.text
    assert response.text.count('<li>') == 61
    assert response.text.count('<strong>公開中</strong>') == 2
    assert "Site 60" in response.text


def test_entry_denies_public_and_untrusted_clients(setup):
    app, _, _, _ = setup
    for headers in (PUBLIC, {"x-forwarded-for": "198.51.100.4", "tailscale-user-login": "test-user-3@example.com"}):
        assert call(app, "GET", "/entry", headers=headers).status_code == 403


def test_map_and_hibi_pages_show_data(setup):
    app, _, _, _ = setup
    map_page = call(app, "GET", "/hq/map", headers=DIRECT)
    hibi_page = call(app, "GET", "/hq/hibi", headers=DIRECT)
    assert map_page.status_code == hibi_page.status_code == 200
    assert "System map" in map_page.text and "Service" in map_page.text and "Request" in map_page.text
    assert "focused" in hibi_page.text and "segments" in hibi_page.text


def test_failed_refresh_displays_failure_last_success_and_old_values(setup):
    app, sites, source, ledger = setup
    for path in ("/entry", "/hq/map", "/hq/hibi"):
        assert "取得しました" in call(app, "GET", path, headers=DIRECT).text
    sites.write_text("invalid json")
    Path(__import__('os').environ["AX_ENTRY_STATE_FILE"]).write_text("invalid json")
    source.write_text("invalid json")
    ledger.failed = True
    for path, expected in (("/entry", "Site 60"), ("/hq/map", "System map"), ("/hq/hibi", "focused")):
        body = call(app, "GET", path, headers=DIRECT).text
        assert "取得に失敗しました" in body and "最終成功時刻: 20" in body
        assert expected in body


def test_hibi_displays_failed_observation_from_timeline(setup, tmp_path):
    from urllib.parse import urlsplit
    from starlette.testclient import TestClient
    from axcommon.client import TimelineClient
    from axcommon.events import build_event
    from axcommon.http import create_app as http_app
    from core.timeline.api import Service, build_routes
    from core.timeline.store import Store

    app, _, _, _ = setup
    store = Store(tmp_path / 'timeline.sqlite')
    service = Service(store, host='test')
    timeline = TestClient(http_app('timeline', '1', routes=build_routes(service, work_side=False), run_heartbeat_loop=False))
    event = build_event(component='hibi-state', host='test', kind='automation.observed', subject='focus',
                        subloop='hibi-state', data={'plugin_id': 'hibi-state', 'target_id': 'focus',
                                                   'as_of': '2030-01-01T00:00:00Z', 'result': 'failed:resources'})
    assert timeline.post('/v1/events', json=event).status_code < 300

    def transport(method, url, headers, body, timeout):
        parsed = urlsplit(url)
        response = timeline.request(method, parsed.path + ('?' + parsed.query if parsed.query else ''), headers=headers, content=body)
        return response.status_code, dict(response.headers), response.content

    app.state.hibi_observation_client = TimelineClient('http://timeline', transport=transport)
    response = call(app, 'GET', '/hq/hibi', headers=DIRECT)
    assert response.status_code == 200
    assert '状態の更新に失敗しました' in response.text and 'focused' in response.text
    app.state.hq_api.read = lambda path: (200, {'ok': True, 'data': {'as_of': '2001-01-01T00:00:00Z', 'state': 'focused'}})
    assert '最後の値は古くなっています' in call(app, 'GET', '/hq/hibi', headers=DIRECT).text
    store.close()


def test_index_timer_persists_failure_and_recovers_through_hq(setup, tmp_path, monkeypatch, repo_root):
    import asyncio
    from runtime.subloop.store import Store
    from runtime.timer_runner.runner import TimerRunner, load_handler

    app, sites, _, _ = setup
    sites.write_text(json.dumps({'sites': [
        {'id': name, 'name': name, 'url': 'https://example.invalid/' + name, 'exposure': 'tailnet'}
        for name in ('sample',)
    ]}))
    root = tmp_path / 'source'
    (root / 'plugins/registry.d').mkdir(parents=True)
    (root / 'compose.d').mkdir()
    (root / 'plugins/registry.yaml').write_text('plugins:\n- {id: sample, kind: timer, path: sample.yaml}\n')
    fragment = root / 'plugins/registry.d/sample.yaml'
    fragment.write_text('plugins:\n- {id: sample, kind: timer, path: sample.yaml}\n')
    source = root / 'compose.d/sample.yaml'
    source.write_text('services:\n  example: {}\n')
    state = tmp_path / 'shared/state.json'
    monkeypatch.setenv('AX_HOME_IN_CONTAINER', str(root))
    monkeypatch.setenv('AX_ENTRY_STATE_FILE', str(state))
    class Sink:
        def emit(self, *args, **kwargs):
            pass
    store = Store(tmp_path / 'timer.sqlite')
    handler = load_handler(repo_root / 'plugins/timer/entry-index')
    async def other(ctx):
        return {'result': 'ok'}
    runner = TimerRunner('timer-runner', store, Sink(), {'entry-index': handler, 'other': other},
                         {'entry-index': {'id': 'entry-index', 'kind': 'timer'},
                          'other': {'id': 'other', 'kind': 'timer'}}, lambda *args: None)
    def run():
        ident = asyncio.run(runner.run_job('entry-index', '2030-01-01T00:00:00Z'))
        return store.get_run(ident)['state']
    assert run() == 'completed'
    first = json.loads(state.read_text())
    body = call(app, 'GET', '/entry', headers=DIRECT).text
    assert '取得しました' in body and 'sample' in body and '収集対象外' in body
    source.write_text('services: [invalid]')
    assert run() == 'failed'
    other_id = asyncio.run(runner.run_job('other', '2030-01-01T00:00:00Z'))
    assert store.get_run(other_id)['state'] == 'completed'
    failed = json.loads(state.read_text())
    assert failed['data'] == first['data'] and failed['last_success_at'] == first['last_success_at']
    body = call(app, 'GET', '/entry', headers=DIRECT).text
    assert '取得に失敗しました' in body and 'sample' in body and first['last_success_at'] in body
    ext = tmp_path / 'ext'
    settings = Settings(public_proxy_ip='192.0.2.1', slots_dir=tmp_path, ui_dir=tmp_path / 'no-ui')
    verifier = AccessVerifier('https://access.ci.invalid', lambda: 'test-aud', lambda: JWKS, clock=lambda: 1000)
    auth = Authenticator(settings.public_proxy_ip, lambda: {'test-user-3@example.com'}, verifier)
    restarted = create_app(settings=settings, authenticator=auth, ext_dir=ext, run_heartbeat_loop=False)
    body = call(restarted, 'GET', '/entry', headers=DIRECT).text
    assert '取得に失敗しました' in body and 'sample' in body and first['last_success_at'] in body
    assert call(restarted, 'GET', '/entry', headers=PUBLIC).status_code == 403
    source.write_text('services:\n  recovered: {}\n')
    sites.write_text(json.dumps({'sites': [
        {'id': 'recovered', 'name': 'recovered', 'url': 'https://example.invalid/recovered', 'exposure': 'tailnet'}
    ]}))
    assert run() == 'completed'
    body = call(restarted, 'GET', '/entry', headers=DIRECT).text
    assert '取得しました' in body and 'recovered' in body
    state.write_text('invalid')
    assert '取得に失敗しました' in call(restarted, 'GET', '/entry', headers=DIRECT).text
    store.close()


def test_index_timer_waits_for_start_delay_and_uses_interval(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone
    from axcommon import timeutil
    from axcommon.defaults import Defaults
    from runtime.subloop.store import Store
    from runtime.timer_runner.runner import TimerRunner

    current = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(timeutil, 'now', lambda: current[0])
    monkeypatch.setattr(timeutil, 'now_ts', lambda: timeutil.format_ts(current[0]))
    seen = []
    async def index(ctx):
        seen.append(current[0])
    class Sink:
        def emit(self, *args, **kwargs):
            pass
    store = Store(tmp_path / 'timer.sqlite')
    manifest = {'id': 'entry-index', 'kind': 'timer', 'schedule': {'kind': 'interval', 'every_s': 3600}}
    runner = TimerRunner('timer-runner', store, Sink(), {'entry-index': index}, {'entry-index': manifest}, lambda *args: None)
    delay, interval = Defaults.load().get('u_entry_start_delay_s'), Defaults.load().get('u_entry_interval_s')
    async def tick():
        await runner.tick()
        await runner.drain()
    asyncio.run(tick())
    assert seen == []
    current[0] += timedelta(seconds=delay - 1)
    asyncio.run(tick())
    assert seen == []
    current[0] += timedelta(seconds=1)
    asyncio.run(tick())
    assert len(seen) == 1
    current[0] += timedelta(seconds=interval)
    asyncio.run(tick())
    assert len(seen) == 2
    store.close()

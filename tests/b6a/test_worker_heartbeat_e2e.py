"""Worker pool startup and recurring health events through the timeline HTTP API."""
import importlib
import time
from urllib.parse import urlsplit

from starlette.testclient import TestClient

from axcommon.client import TimelineClient, Unavailable
from axcommon.http import create_app
from core.timeline.api import Service, build_routes
from core.timeline.store import Store


class Control:
    def get(self, path):
        return {'mode': 'poll_paused', 'revision': 1, 'knobs': {}}


class Resources:
    def post(self, path, body):
        return {}


def test_pool_heartbeat_reaches_timeline_and_recovers_from_outage(tmp_path, monkeypatch):
    timeline_store = Store(tmp_path / 'timeline.sqlite')
    timeline_service = Service(timeline_store, host='test')
    timeline_app = create_app('timeline', '1', routes=build_routes(timeline_service, work_side=False), run_heartbeat_loop=False)
    timeline = TestClient(timeline_app)
    network = {'down': False}
    def transport(method, url, headers, body, timeout):
        if network['down']:
            raise Unavailable('offline')
        parsed = urlsplit(url)
        response = timeline.request(method, parsed.path + ('?' + parsed.query if parsed.query else ''), headers=headers, content=body)
        return response.status_code, dict(response.headers), response.content

    monkeypatch.setattr('axcommon.client.TimelineClient', lambda *args, **kwargs: TimelineClient('http://timeline', transport=transport))
    monkeypatch.setenv('AX_WORKER_POOL_EVENT_OUTBOX', str(tmp_path / 'outbox'))
    pool = importlib.import_module('plugins.resource.worker-pool.app')
    core, work, service = pool.build(pool.Config(db=str(tmp_path / 'pool.sqlite'), host='test-host'), resources=Resources(), control=Control(), dispatch_interval_s=60)
    core.state.heartbeat.interval_s = .05
    with TestClient(core) as client:
        assert client.get('/healthz').status_code == 200
        def events():
            response = timeline.get('/v1/events', params={'kind': 'health.beat'})
            assert response.status_code == 200
            return response.json()['events']
        deadline = time.monotonic() + 3
        while len(events()) < 2 and time.monotonic() < deadline:
            time.sleep(.02)
        rows = events()
        assert len(rows) >= 2
        assert all(row['source']['component'] == 'worker-pool' and row['source']['host'] == 'test-host' for row in rows)
        network['down'] = True
        deadline = time.monotonic() + 3
        while len(core.state.heartbeat.sink.outbox) == 0 and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(core.state.heartbeat.sink.outbox) > 0
        assert client.get('/healthz').status_code == 200
        before = len(rows)
        network['down'] = False
        deadline = time.monotonic() + 3
        while (len(events()) <= before or len(core.state.heartbeat.sink.outbox)) and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(events()) > before
        assert len(core.state.heartbeat.sink.outbox) == 0
        with TestClient(work) as work_client:
            assert work_client.get('/healthz').status_code == 200
            assert work.state.heartbeat.sink is None
    service.store.close()
    timeline_store.close()

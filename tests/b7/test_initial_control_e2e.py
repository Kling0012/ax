import threading
import time

from starlette.testclient import TestClient

from runtime.subloop.app import build_app
from tests.b7.exhelp import graph, make_executor, manifest, node
from tests.fakes.b7 import control, make_env


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('condition not reached')


def test_runtime_waits_for_valid_control_and_reconnects(tmp_path, repo_root, fake_clock):
    env = make_env(tmp_path, repo_root, fake_clock)
    env.svc.defaults._t['control_poll_s']['default'] = 0.02
    env.store.set_meta('mode', 'run')
    entered, release = threading.Event(), threading.Event()
    state = {'response': 'delayed', 'calls': 0}

    def effective(_pid):
        state['calls'] += 1
        if state['response'] == 'delayed':
            entered.set()
            release.wait(2)
            raise ConnectionError('unavailable')
        if state['response'] == 'failure':
            raise PermissionError('unauthorized')
        return {'mode': state['response'], 'revision': 1, 'knobs': {}}

    env.control.effective = effective
    # These nodes stand in for the two externally effective actions; neither invokes a network.
    definition = graph([node('publish'), node('push')], [{'from': 'publish', 'to': 'push'}])
    ex = make_executor(env, definition, manifest(outputs=[]))
    app = build_app(ex, apply_token='token', tick_s=0.01)
    headers = {'Authorization': 'Bearer token'}
    with TestClient(app) as client:
        assert entered.wait(1)
        assert client.get('/status').json()['mode'] == 'unknown'
        assert not client.post('/apply', headers=headers, json={'mode': 'run', 'revision': 1, 'run_now': True}).json()['applied']
        assert not control.LOG
        state['response'] = 'failure'
        release.set()
        wait_for(lambda: state['calls'] >= 3)
        assert client.get('/status').json()['mode'] == 'unknown'
        assert not control.LOG
        state['response'] = 'invalid'
        wait_for(lambda: state['calls'] >= 5)
        assert client.get('/status').json()['mode'] == 'unknown'
        assert not control.LOG
        state['response'] = 'collect_only'
        wait_for(lambda: client.get('/status').json()['mode'] == 'collect_only')
        assert client.post('/apply', headers=headers, json={'mode': 'collect_only', 'revision': 1, 'run_now': True}).json()['applied']
        time.sleep(0.1)
        assert not control.LOG
        state['response'] = 'run'
        wait_for(lambda: client.get('/status').json()['mode'] == 'run')
        assert client.post('/apply', headers=headers, json={'mode': 'run', 'revision': 1, 'run_now': True}).json()['applied']
        wait_for(lambda: {'publish', 'push'} <= {name for _, name in control.LOG})
        state['response'] = 'failure'
        calls = state['calls']
        wait_for(lambda: state['calls'] >= calls + 2)
        state['response'] = 'collect_only'
        wait_for(lambda: client.get('/status').json()['mode'] == 'collect_only')

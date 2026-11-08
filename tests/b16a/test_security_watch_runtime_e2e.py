import asyncio
import json

from axcommon.client import Unavailable
from runtime.subloop.store import Store
from runtime.timer_runner.__main__ import SecurityWatchFactory
from runtime.timer_runner.runner import TimerRunner, load_handler


def test_security_watch_credentials_api_failure_and_notification_are_isolated(tmp_path, repo_root):
    baselines = tmp_path / 'baselines.json'
    baselines.write_text(json.dumps({'host-a': {'version': 'v1', 'values': {'value': 'safe'}}}))
    events = []
    class Sink:
        def emit(self, kind, subject, data=None, **kwargs):
            events.append((kind, subject, data))
    credential = [None]
    unavailable = [True]
    def token_reader(name):
        assert name == 'plugin-token-security-watch'
        return credential[0]
    def transport(method, url, headers, body, timeout):
        assert headers['Authorization'] == 'Bearer test-token'
        if unavailable[0]:
            raise Unavailable('offline')
        if method == 'POST':
            assert json.loads(body)['entry']['name'] == 'audit-readonly'
            return 202, {}, b'{}'
        return 200, {}, json.dumps({'state': 'succeeded', 'results': {
            'host-a': {'status': 'ok', 'values': {'value': 'different'}}}}).encode()
    factory = SecurityWatchFactory(tmp_path, environ={'AX_SECURITY_BASELINES_FILE': str(baselines)},
                                   token_reader=token_reader, transport=transport)
    seen = []
    async def other(ctx):
        seen.append('ok')
        return {'result': 'ok'}
    todos = []
    class Ledger:
        def post(self, path, body):
            assert path == '/v1/todos'
            todos.append(body)
    handlers = {'security-watch': load_handler(repo_root / 'plugins/timer/security-watch'), 'other': other}
    store = Store(tmp_path / 'timer.sqlite')
    timer = TimerRunner('timer-runner', store, Sink(), handlers,
                        {name: {'id': name, 'kind': 'timer'} for name in handlers},
                        lambda pid, name: Ledger() if name == 'ledger' else None,
                        extra_clients={'security-watch': {'security_watch': factory}})
    async def exercise():
        missing = await timer.run_job('security-watch', '2026-11-08T00:00:00Z')
        other_a = await timer.run_job('other', '2026-11-08T00:00:00Z')
        credential[0] = 'test-token'
        offline = await timer.run_job('security-watch', '2026-11-08T00:01:00Z')
        other_b = await timer.run_job('other', '2026-11-08T00:01:00Z')
        unavailable[0] = False
        available = await timer.run_job('security-watch', '2026-11-08T00:02:00Z')
        return missing, other_a, offline, other_b, available
    missing, other_a, offline, other_b, available = asyncio.run(exercise())
    assert store.get_run(missing)['reason'] == 'SecurityCredentialUnavailable'
    assert all(store.get_run(r)['state'] == 'completed' for r in (other_a, offline, other_b, available))
    assert seen == ['ok', 'ok']
    notices = [data for kind, _, data in events if kind == 'change.notification']
    assert len(notices) == 1 and notices[0]['state'] == 'requested'
    assert len(todos) == 1 and todos[0]['state'] == 'todo'
    assert not any('approval' in kind for kind, _, _ in events)
    store.close()


def test_failed_resource_job_never_records_clean_result(tmp_path, repo_root):
    baselines = tmp_path / 'baselines.json'
    baselines.write_text(json.dumps({'host-a': {'version': 'v1', 'values': {'value': 'safe'}}}))
    events = []

    class Sink:
        def emit(self, kind, subject, data=None, **kwargs):
            events.append((kind, subject, data))

    def transport(method, url, headers, body, timeout):
        if method == 'POST':
            return 202, {}, b'{}'
        return 200, {}, json.dumps({'state': 'failed', 'results': {
            'host-a': {'status': 'ok', 'values': {'value': 'safe'}}}}).encode()

    factory = SecurityWatchFactory(tmp_path, environ={'AX_SECURITY_BASELINES_FILE': str(baselines)},
                                   token_reader=lambda name: 'test-token', transport=transport)
    store = Store(tmp_path / 'timer.sqlite')
    timer = TimerRunner('timer-runner', store, Sink(),
                        {'security-watch': load_handler(repo_root / 'plugins/timer/security-watch')},
                        {'security-watch': {'id': 'security-watch', 'kind': 'timer'}},
                        lambda *args: None, extra_clients={'security-watch': {'security_watch': factory}})
    asyncio.run(timer.run_job('security-watch', '2026-11-08T00:00:00Z'))
    import sqlite3
    db = sqlite3.connect(tmp_path / 'security-watch' / 'monitor.sqlite3')
    value = json.loads(db.execute("SELECT value FROM state WHERE key = ?", ('security:host-a',)).fetchone()[0])
    assert value['status'] == 'unavailable'
    assert not any(kind == 'change.notification' for kind, _, _ in events)
    db.close()
    store.close()


def test_tumu_timer_ssh_failure_recovery_delivery_and_collect_only(tmp_path, repo_root):
    from types import SimpleNamespace
    from core.hq.notify.dispatcher import Dispatcher
    from core.hq.notify.guard import Verdict
    from core.hq.notify.ntfy import Receipt
    from core.hq.notify.store import NotifyStore
    from plugins.timer.b16a_monitor.delivery import MonitorDelivery
    from plugins.timer.b16a_monitor.tumu import TumuSSHReader
    from runtime.timer_runner.__main__ import TumuWatchFactory

    targets = tmp_path / 'targets.json'
    targets.write_text(json.dumps({'phone': {'host': 'fake-host', 'user': 'reader',
                                             'path': '/readonly/status.json', 'identity_file': '/fake/key'}}))
    calls, deliveries, todos = [], [], []
    reply = [255]
    def ssh(argv, **kwargs):
        calls.append(argv)
        assert 'adb' in argv[-1] and 'tail -c 3000000' in argv[-1]
        return SimpleNamespace(returncode=reply[0], stdout=b'4070908800\n2099-01-01T00:00:00.000+00:00\t1\t2\t3\ttick\tautoplay tick\tplaying\n', stderr=b'')
    class Sender:
        def send(self, **kwargs):
            deliveries.append(kwargs)
            return Receipt(True, id='fake-receipt')
    class Guard:
        def inspect(self, title, message, policy):
            return Verdict('send', title, message)
    class Sink:
        def emit(self, *args, **kwargs):
            pass
    class Ledger:
        def post(self, path, body):
            todos.append(body)
    notify_store = NotifyStore(tmp_path / 'notifications.sqlite')
    delivery = MonitorDelivery(Dispatcher(notify_store, {'ntfy': Sender()}, guard=Guard()))
    reader = TumuSSHReader(json.loads(targets.read_text()), runner=ssh)
    factory = TumuWatchFactory(tmp_path, environ={'AX_TUMU_TARGETS_FILE': str(targets)},
                               reader=reader, notifier=delivery)
    store = Store(tmp_path / 'timer.sqlite')
    other = []
    async def other_handler(ctx):
        other.append(True)
    handlers = {'tumu-watch': load_handler(repo_root / 'plugins/timer/tumu-watch'), 'other': other_handler}
    timer = TimerRunner('timer-runner', store, Sink(), handlers,
                        {n: {'id': n, 'kind': 'timer'} for n in handlers},
                        lambda *args: Ledger(), extra_clients={'tumu-watch': {'tumu_watch': factory}})
    async def run():
        await timer.run_job('tumu-watch', '2026-11-08T00:00:00Z')
        await timer.run_job('other', '2026-11-08T00:00:00Z')
        await timer.run_job('tumu-watch', '2026-11-08T00:01:00Z')
        await timer.run_job('tumu-watch', '2026-11-08T00:02:00Z')
        reply[0] = 0
        await timer.run_job('tumu-watch', '2026-11-08T00:03:00Z')
        timer.set_effective('tumu-watch', {'mode': 'collect_only', 'revision': 1})
        await timer.run_job('tumu-watch', '2026-11-08T00:04:00Z')
    asyncio.run(run())
    import sqlite3
    db = sqlite3.connect(tmp_path / 'tumu-watch' / 'monitor.sqlite3')
    state = json.loads(db.execute('SELECT value FROM state WHERE key = ?', ('tumu:phone',)).fetchone()[0])
    assert state['status'] == 'ok' and state['failures'] == 0
    assert len(calls) == 4 and len(deliveries) == 1 and other == [True] and not todos
    db.close()
    notify_store.close()
    store.close()


def test_tumu_unconfigured_and_delivery_failure_stay_unconfirmed(tmp_path, repo_root):
    from types import SimpleNamespace
    import sqlite3
    from core.hq.notify.dispatcher import Dispatcher
    from core.hq.notify.guard import Verdict
    from core.hq.notify.ntfy import Receipt
    from core.hq.notify.store import NotifyStore
    from plugins.timer.b16a_monitor.delivery import MonitorDelivery
    from plugins.timer.b16a_monitor.tumu import TumuSSHReader
    from runtime.timer_runner.__main__ import TumuWatchFactory

    class Sink:
        def emit(self, *args, **kwargs):
            pass
    class Guard:
        def inspect(self, title, message, policy):
            return Verdict('send', title, message)
    class Sender:
        def send(self, **kwargs):
            return Receipt(False, transient=True, reason='offline')
    class Ledger:
        def post(self, path, body):
            pass
    notify_store = NotifyStore(tmp_path / 'delivery.sqlite')
    delivery = MonitorDelivery(Dispatcher(notify_store, {'ntfy': Sender()}, guard=Guard()))
    store = Store(tmp_path / 'timer.sqlite')
    factory = TumuWatchFactory(tmp_path, environ={})
    handler = load_handler(repo_root / 'plugins/timer/tumu-watch')
    timer = TimerRunner('timer-runner', store, Sink(), {'tumu-watch': handler},
                        {'tumu-watch': {'id': 'tumu-watch', 'kind': 'timer'}},
                        lambda *args: Ledger(), extra_clients={'tumu-watch': {'tumu_watch': factory}})
    asyncio.run(timer.run_job('tumu-watch', '2026-11-08T00:00:00Z'))
    db = sqlite3.connect(tmp_path / 'tumu-watch' / 'monitor.sqlite3')
    state = json.loads(db.execute('SELECT value FROM state WHERE key = ?', ('tumu:unconfigured',)).fetchone()[0])
    assert state['status'] == 'error'
    targets = tmp_path / 'targets.json'
    data = {'phone': {'host': 'fake-host', 'user': 'reader', 'path': '/status', 'identity_file': '/fake/key'}}
    targets.write_text(json.dumps(data))
    reader = TumuSSHReader(data, runner=lambda *a, **k: SimpleNamespace(returncode=255, stdout=b'', stderr=b''))
    timer.extra_clients['tumu-watch']['tumu_watch'] = TumuWatchFactory(
        tmp_path, environ={'AX_TUMU_TARGETS_FILE': str(targets)}, reader=reader, notifier=delivery)
    async def poll():
        await timer.run_job('tumu-watch', '2026-11-08T00:01:00Z')
        await timer.run_job('tumu-watch', '2026-11-08T00:02:00Z')
    asyncio.run(poll())
    state = json.loads(db.execute('SELECT value FROM state WHERE key = ?', ('tumu:phone',)).fetchone()[0])
    notice = json.loads(db.execute('SELECT value FROM state WHERE key = ?',
                                   ('notice:tumu:phone:unreachable:1',)).fetchone()[0])
    assert state['status'] == 'unreachable' and state['failures'] == 2
    assert notice['status'] != 'sent'
    db.close()
    notify_store.close()
    store.close()


def test_security_difference_reaches_fake_ntfy_once(tmp_path, repo_root, monkeypatch):
    from types import SimpleNamespace
    import axcommon.redact
    from runtime.timer_runner.__main__ import SecurityWatchFactory

    class Redactor:
        index_ok = pii_list_ok = True
        def redact_text(self, text):
            return SimpleNamespace(text=text, hits=[])
    monkeypatch.setattr(axcommon.redact, 'from_environment', lambda: Redactor())
    baselines = tmp_path / 'baselines.json'
    baselines.write_text(json.dumps({'host-a': {'version': 'v1', 'values': {'value': 'safe'}}}))
    sent = []
    def transport(method, url, headers, body, timeout):
        if 'fake-ntfy' in url:
            sent.append(json.loads(body))
            return 200, {}, b'{"id":"fake-receipt"}'
        if method == 'POST':
            return 202, {}, b'{}'
        return 200, {}, json.dumps({'state': 'succeeded', 'results': {
            'host-a': {'status': 'ok', 'values': {'value': 'different'}}}}).encode()
    class Sink:
        def emit(self, *args, **kwargs):
            pass
    class Ledger:
        def post(self, path, body):
            raise AssertionError('successful delivery must not create a failure todo')
    factory = SecurityWatchFactory(tmp_path, environ={'AX_SECURITY_BASELINES_FILE': str(baselines),
                                   'AX_NTFY_ENDPOINT': 'http://fake-ntfy'},
                                   token_reader=lambda name: 'test-token', transport=transport)
    store = Store(tmp_path / 'timer.sqlite')
    timer = TimerRunner('timer-runner', store, Sink(),
                        {'security-watch': load_handler(repo_root / 'plugins/timer/security-watch')},
                        {'security-watch': {'id': 'security-watch', 'kind': 'timer'}},
                        lambda *args: Ledger(), extra_clients={'security-watch': {'security_watch': factory}})
    async def poll():
        await timer.run_job('security-watch', '2026-11-08T00:00:00Z')
        await timer.run_job('security-watch', '2026-11-08T00:01:00Z')
    asyncio.run(poll())
    assert len(sent) == 1 and 'security-difference' in sent[0]['message']
    store.close()


def test_tumu_trace_observations_through_monitor(tmp_path):
    from types import SimpleNamespace
    from plugins.timer.b16a_monitor import Monitor
    from plugins.timer.b16a_monitor.tumu import TumuSSHReader

    results = []
    class Notifier:
        def send(self, key, kind, target, detail):
            results.append(kind)
            return 'sent'
    class Ledger:
        def record(self, *args):
            raise AssertionError('notification should succeed')
    response = [b'']
    def ssh(argv, **kwargs):
        assert 'tail -c 3000000' in argv[-1]
        assert 'adb' in argv[-1]
        return SimpleNamespace(returncode=0, stdout=response[0], stderr=b'')
    reader = TumuSSHReader({'phone': {'host': 'fake-host', 'user': 'reader',
                            'identity_file': '/fake/key', 'adb_serial': 'fake:5555'}}, runner=ssh)
    monitor = Monitor(str(tmp_path / 'trace.sqlite'), tumu=reader, environment_jobs=None,
                      notifier=Notifier(), ledger=Ledger())
    def trace(at, detail='playing'):
        return ('4070909400\n' + at + '.000+00:00\t1\t2\t3\ttick\tautoplay tick\t' + detail + '\n').encode()
    response[0] = trace('2099-01-01T00:10:00').replace(b'.000+00:00', b'.999+00:00')
    assert monitor.poll_tumu('phone')['stalled'] is False
    assert results == []
    response[0] = trace('2099-01-01T00:05:00')
    assert monitor.poll_tumu('phone')['stalled'] is False
    response[0] = trace('2099-01-01T00:04:59')
    assert monitor.poll_tumu('phone')['stalled'] is True
    assert results == ['tumu-stalled']
    response[0] = trace('2099-01-01T00:09:59')
    assert monitor.poll_tumu('phone')['stalled'] is False
    response[0] = trace('2099-01-01T00:09:59', 'loop stopped')
    assert monitor.poll_tumu('phone')['stalled'] is True
    assert results == ['tumu-stalled', 'tumu-stalled']
    response[0] = b''
    assert monitor.poll_tumu('phone')['status'] == 'error'
    response[0] = b'4070909400\n2099-01-01T00:09:59\t1\t2\t3\ttick\tautoplay tick\tplaying\n'
    assert monitor.poll_tumu('phone')['status'] == 'error'
    response[0] = b'4070909400\nmalformed autoplay tick\n'
    assert monitor.poll_tumu('phone')['status'] == 'error'
    assert results == ['tumu-stalled', 'tumu-stalled']

    reader.targets['phone']['stall_threshold_s'] = 60
    response[0] = trace('2099-01-01T00:09:59')
    assert monitor.poll_tumu('phone')['stalled'] is False
    response[0] = trace('2099-01-01T00:08:59')
    assert monitor.poll_tumu('phone')['stalled'] is True
    assert results == ['tumu-stalled', 'tumu-stalled', 'tumu-stalled']

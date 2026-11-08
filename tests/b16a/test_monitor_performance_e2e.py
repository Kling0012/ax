from plugins.timer.b16a_monitor import Monitor
from plugins.subloop.performance import Performance


class FakeTumu:
    def __init__(self):
        self.next = {'status': 'unreachable'}

    def read(self, target):
        return self.next


class FakeJobs:
    def __init__(self):
        self.results = {}
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.results


class FakeNotifier:
    def __init__(self):
        self.calls = []
        self.result = 'sent'

    def send(self, key, kind, target, detail):
        self.calls.append((key, kind, target, detail))
        return self.result


class FakeLedger:
    def __init__(self):
        self.calls = []

    def record(self, *args):
        self.calls.append(args)


def test_t68_monitor_e2e(tmp_path):
    tumu, jobs, notifier, ledger = FakeTumu(), FakeJobs(), FakeNotifier(), FakeLedger()
    path = str(tmp_path / 'monitor.sqlite')
    monitor = Monitor(path, tumu=tumu, environment_jobs=jobs, notifier=notifier, ledger=ledger)
    assert monitor.poll_tumu('phone')['failures'] == 1
    assert not notifier.calls
    tumu.next = {'status': 'ok'}
    assert monitor.poll_tumu('phone')['status'] == 'error'
    assert monitor.status('tumu:phone')['failures'] == 1
    tumu.next = {'status': 'unreachable'}
    assert monitor.poll_tumu('phone')['failures'] == 2
    assert len(notifier.calls) == 1
    monitor.poll_tumu('phone')
    assert len(notifier.calls) == 1
    tumu.next = {'status': 'ok', 'stalled': False}
    assert monitor.poll_tumu('phone')['failures'] == 0
    tumu.next = {'status': 'unreachable'}
    monitor.poll_tumu('phone')
    assert len(notifier.calls) == 1
    monitor.poll_tumu('phone')
    assert len(notifier.calls) == 2
    baselines = {'host-a': {'version': 'v1', 'values': {'password_auth': False}},
                 'host-b': {'version': 'v1', 'values': {'password_auth': False}}}
    jobs.results = {'host-a': {'status': 'ok', 'values': {'password_auth': True}},
                    'host-b': {'status': 'error'}}
    notifier.result = 'failed'
    result = monitor.poll_security(baselines)
    assert jobs.calls[-1] == {'requires': ['security-audit'], 'scope': 'each', 'entry': 'audit-readonly'}
    assert result['host-a']['status'] == 'different'
    assert result['host-b']['status'] == 'unavailable'
    key = notifier.calls[-1][0]
    assert monitor.status('notice:' + key)['status'] == 'pending'
    assert ledger.calls
    notifier.result = 'sent'
    monitor = Monitor(path, tumu=tumu, environment_jobs=jobs, notifier=notifier, ledger=ledger)
    monitor.poll_security(baselines)
    assert monitor.status('notice:' + key)['status'] == 'sent'
    count = len(notifier.calls)
    monitor.poll_security(baselines)
    assert len(notifier.calls) == count
    # Collect-only performs no observation or notification, and missing data never means clean.
    assert monitor.poll_security(baselines, mode='collect_only')['status'] == 'skipped'
    assert len(notifier.calls) == count
    jobs.results = {'host-a': {'status': 'ok', 'values': {}}}
    assert monitor.poll_security(baselines)['host-a']['status'] == 'unavailable'
    assert monitor.status('security:host-a')['status'] == 'unavailable'
    tumu.next = {'status': 'ok', 'stalled': True}
    monitor.poll_tumu('phone')
    assert notifier.calls[-1][1] == 'tumu-stalled'


def test_t68_unknown_notification_e2e(tmp_path):
    tumu, jobs, notifier, ledger = FakeTumu(), FakeJobs(), FakeNotifier(), FakeLedger()
    notifier.result = 'unknown'
    monitor = Monitor(str(tmp_path / 'monitor.sqlite'), tumu=tumu, environment_jobs=jobs, notifier=notifier, ledger=ledger)
    jobs.results = {'host': {'status': 'ok', 'values': {'setting': 1}}}
    baseline = {'host': {'version': 'v1', 'values': {'setting': 0}}}
    monitor.poll_security(baseline)
    key = notifier.calls[0][0]
    monitor.poll_security(baseline)
    assert len(notifier.calls) == 1
    assert monitor.status('notice:' + key)['status'] == 'unknown'
    monitor.reconcile(key, 'pending')
    notifier.result = 'sent'
    monitor.poll_security(baseline)
    assert monitor.status('notice:' + key)['status'] == 'sent'


class FakeBackend:
    def __init__(self):
        self.started = []
        self.prs = []

    def prepare(self, target, item):
        self.started.append((target, item))
        return {'id': 'change-1'}

    def create_pr(self, target, item, change):
        self.prs.append((target, item, change))
        return 'pr-1'


def test_t69_stop_and_explicit_resume_e2e(tmp_path):
    backend = FakeBackend()
    path = str(tmp_path / 'perf.sqlite')
    perf = Performance(path, backend)
    for trigger in ('schedule', 'run_now', 'manager', 'inspection', 'emergency'):
        assert perf.run('repo-a', 'item-1', trigger=trigger)['status'] == 'stopped'
    for issuer in ('manager', 'inspection', 'emergency'):
        assert not perf.resume('repo-a', issuer=issuer, explicit=True)
    assert not perf.resume('repo-a', issuer='user', explicit=False)
    assert not perf.resume('all', issuer='user', explicit=True)
    assert perf.run('repo-a', 'item-1', trigger='schedule', mode='collect_only')['status'] == 'stopped'
    assert backend.started == [] and backend.prs == []
    assert perf.resume('repo-a', issuer='user', explicit=True)
    perf = Performance(path, backend)
    assert perf.run('repo-a', 'item-1', trigger='run_now', mode='collect_only')['status'] == 'stopped'
    assert backend.started == [] and backend.prs == []
    assert perf.run('repo-b', 'item-1', trigger='run_now')['status'] == 'stopped'
    assert perf.run('repo-a', 'item-1', trigger='run_now')['status'] == 'sent'
    assert perf.run('repo-a', 'item-1', trigger='schedule')['status'] == 'sent'
    assert len(backend.prs) == 1
    perf.stop('repo-a')
    assert perf.run('repo-a', 'item-2', trigger='emergency')['status'] == 'stopped'
    assert len(backend.prs) == 1

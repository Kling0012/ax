import json

import pytest

from axcommon.client import Unavailable
from plugins.timer.b16a_monitor.environment_jobs import EnvironmentJobsClient, create_monitor
from tests.b16a.test_monitor_performance_e2e import FakeLedger, FakeNotifier, FakeTumu


def test_monitor_through_resource_api(tmp_path):
    requests = []

    def transport(method, url, headers, body, timeout):
        requests.append((method, url, headers, body))
        assert headers.get('Authorization') == 'Bearer test-token'
        if method == 'POST':
            job = json.loads(body)
            assert job['requires'] == ['security-audit'] and job['scope'] == 'each'
            assert job['entry']['name'] == 'audit-readonly'
            return 202, {}, b'{}'
        return 200, {}, json.dumps({'state': 'succeeded', 'results': {
            'host-a': {'status': 'ok', 'values': {'password_auth': True}},
            'host-b': {'status': 'error'},
        }}).encode()

    notifier = FakeNotifier()
    monitor = create_monitor(str(tmp_path / 'monitor.db'), resources_url='http://resources:8081',
                             token='test-token', tumu=FakeTumu(), notifier=notifier,
                             ledger=FakeLedger(), transport=transport)
    baselines = {host: {'version': 'v1', 'values': {'password_auth': False}}
                 for host in ('host-a', 'host-b')}
    result = monitor.poll_security(baselines)
    assert result['host-a']['status'] == 'different'
    assert result['host-b']['status'] == 'unavailable'
    assert len(notifier.calls) == 1
    assert [r[0] for r in requests] == ['POST', 'GET']


def test_unavailable_resources_does_not_stop_tumu(tmp_path):
    def unavailable(*args):
        raise Unavailable('unavailable')

    tumu = FakeTumu()
    tumu.next = {'status': 'ok', 'stalled': False}
    monitor = create_monitor(str(tmp_path / 'monitor.db'), resources_url='http://resources:8081',
                             token='test-token', tumu=tumu, notifier=FakeNotifier(),
                             ledger=FakeLedger(), transport=unavailable)
    assert monitor.poll_security({'host': {'version': 'v1', 'values': {'x': 1}}})['host']['status'] == 'unavailable'
    assert monitor.poll_tumu('phone')['status'] == 'ok'


def test_missing_credential_is_rejected():
    with pytest.raises(ValueError):
        EnvironmentJobsClient('http://resources:8081', None)

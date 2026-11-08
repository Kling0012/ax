from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat

import pytest

from core.hq.approval.factor import verify_code
from core.hq.approval.private import PrivateStore
from core.hq.approval.service import ApprovalFailure, FactorService
from core.hq.approval.qr import matrix, svg
from tests.fakes.b12b2.hq.parts import Forms, call, fake_checker

RFC_SEED = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'


@pytest.mark.parametrize('now,code', [(59, '287082'), (1111111109, '081804'),
    (1111111111, '050471'), (1234567890, '005924'), (2000000000, '279037'), (20000000000, '353130')])
def test_totp_rfc6238_sha1_vectors(now, code):
    assert verify_code(RFC_SEED, code, now, -1) == now // 30


def test_t190_totp_window_and_expiry():
    assert verify_code(RFC_SEED, '287082', 29, -1) == 1
    assert verify_code(RFC_SEED, '287082', 89, -1) == 1
    assert verify_code(RFC_SEED, '287082', 90, -1) is None
    assert verify_code(RFC_SEED, '287082', 59, 1) is None


@pytest.mark.parametrize('code', [287082, None, '', '287082\n', '２８７０８２', '0287082', ' 287082'])
def test_totp_strict_six_ascii_digits(code):
    assert verify_code(RFC_SEED, code, 59, -1) is None


def test_t190_replay_and_lock_survive_restart(world):
    world['factor'].verify(world['identity'], '123456')
    again = FactorService(PrivateStore(world['store'].directory), clock=lambda: world['now'][0],
                          checker=fake_checker, emit=world['events'].append, notify=world['notifications'].append)
    with pytest.raises(ApprovalFailure):
        again.verify(world['identity'], '123456')
    for _ in range(4):
        with pytest.raises(ApprovalFailure):
            again.verify(world['identity'], '999999')
    third = FactorService(PrivateStore(world['store'].directory), clock=lambda: world['now'][0],
                          checker=fake_checker, emit=world['events'].append, notify=world['notifications'].append)
    world['now'][0] += 30
    with pytest.raises(ApprovalFailure) as err:
        third.verify(world['identity'], '123456')
    assert err.value.code == 'approval_locked'


def test_t190_concurrent_code_consumption_once(world):
    def verify(_):
        other = FactorService(PrivateStore(world['store'].directory), clock=lambda: world['now'][0],
                              checker=fake_checker, emit=world['events'].append, notify=world['notifications'].append)
        try:
            other.verify(world['identity'], '123456')
            return True
        except ApprovalFailure:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(verify, range(4))) == 1


def test_t211_private_paths_modes_and_no_cross_boundary_writes(world):
    private = world['store'].directory
    for path in (private.parent, private):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert path.stat().st_uid == os.geteuid()
    assert stat.S_IMODE((private / 'approval-totp').stat().st_mode) == 0o600
    world['factor'].verify(world['identity'], '123456')
    paths = [p.relative_to(world['root']) for p in world['root'].rglob('*') if p.is_file()]
    assert paths
    assert all(str(p).startswith('hq/private/') for p in paths)
    assert not any('seats.yaml' in str(p) or 'gate' in str(p) or 'intake' in str(p) for p in paths)
    output = json.dumps(world['events'] + world['notifications'])
    assert 'seed' not in output and '123456' not in output


@pytest.mark.parametrize('target', ['hq', 'private', 'approval-totp'])
def test_t211_reject_bad_permissions(world, target):
    private = world['store'].directory
    path = {'hq': private.parent, 'private': private, 'approval-totp': private / 'approval-totp'}[target]
    path.chmod(0o770 if path.is_dir() else 0o640)
    with pytest.raises(PermissionError):
        world['factor'].status()
    assert call(world['app'], 'GET', '/approval/settings')['status'] == 503


def test_t211_reject_private_symlink(tmp_path):
    hq, elsewhere = tmp_path / 'hq', tmp_path / 'elsewhere'
    hq.mkdir(mode=0o700)
    elsewhere.mkdir(mode=0o700)
    (hq / 'private').symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(PermissionError):
        with PrivateStore(hq / 'private').locked():
            pass
    assert not list(elsewhere.iterdir())


def test_t211_reset_and_host_added_warnings_expire(world):
    for kind in ('approval.factor.reset', 'seat.host_added'):
        world['factor'].observe_host_event(event_id='fake-' + kind, kind=kind, at=world['now'][0])
    assert len(world['factor'].status()['warnings']) == 2
    assert 'approval.factor.reset' in call(world['app'], 'GET', '/seats')['text']
    assert 'seat.host_added' in call(world['app'], 'GET', '/approval/settings')['text']
    world['now'][0] += 86400
    assert world['factor'].status()['warnings'] == []
    assert call(world['app'], 'POST', '/v1/hq/approval/reset', body={})['status'] == 404


def test_enrollment_rotation_needs_current_code_and_qr_shown_once(world):
    page = Forms(call(world['app'], 'GET', '/approval/settings')['text']).values
    reply = call(world['app'], 'POST', '/v1/hq/approval/enroll', body={**page, 'code': '000000'})
    assert reply['status'] == 403
    page = Forms(call(world['app'], 'GET', '/approval/settings')['text']).values
    reply = call(world['app'], 'POST', '/v1/hq/approval/enroll', body={**page, 'code': '123456'})
    assert reply['status'] == 200 and 'data:image/svg+xml;base64,' in reply['text']
    assert call(world['app'], 'POST', '/v1/hq/approval/enroll', body={**page, 'code': '123456'})['status'] == 403
    assert 'data:image' not in call(world['app'], 'GET', '/approval/settings')['text']
    assert [e['kind'] for e in world['events']].count('approval.factor.enrolled') == 2
    assert all(not e['data'] for e in world['events'] if e['kind'] == 'approval.factor.enrolled')


def test_notification_outbox_is_private_and_retries_same_id(world):
    def broken(item):
        raise ConnectionError('fake')
    world['factor'].notify = broken
    with pytest.raises(ApprovalFailure):
        world['factor'].verify(world['identity'], '000000')
    pending = world['store'].read()['outbox']
    assert len(pending) == 1
    event_id = pending[0]['id']
    world['factor'].notify = world['notifications'].append
    assert world['factor'].status()['notification_pending'] is False
    assert world['notifications'][-1]['id'] == event_id
    assert 'seed' not in json.dumps(world['notifications'])


def test_qr_structure_capacity_and_xml_escaping():
    # Independent geometry assertions for ISO version 8-L. Full optical scan
    # remains a real-device check; no browser or service is launched here.
    cells = matrix('otpauth://totp/AX:approval?secret=' + RFC_SEED)
    assert len(cells) == 49 and all(len(row) == 49 for row in cells)
    assert all(isinstance(value, bool) for row in cells for value in row)
    for top, left in ((0, 0), (42, 0), (0, 42)):
        assert all(cells[top][left + j] and cells[top + 6][left + j] for j in range(7))
        assert cells[top + 3][left + 3] and not cells[top + 1][left + 1]
    value = svg('<script>not-html</script>')
    assert '<script>' not in value and 'viewBox="0 0 57 57"' in value
    with pytest.raises(ValueError):
        matrix('x' * 193)

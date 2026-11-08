from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path

import pytest

from tools.cutover.retirement import (
    Context, PRECONDITIONS, StepFailure, USER_GROUPS, X_GROUPS, run_retirement, stop_set,
)
from tools.cutover.retirement import fsops
from tests.fakes.b17.retirement.world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def notes(world, g):
    return [e for e in world.events if e['subject'] == f'issue-automation-old/{g}']


def test_t141_order_manifest_and_no_deletion(world):
    result = run_retirement(world.context(), world.profile)
    assert result.condition1 and result.archive_to_hdd_stopped
    assert len(result.steps) == 20
    first = world.stops()[0]
    assert first[1] == world.h['compute-host'].name
    assert first[2][-1] == 'issue-automation-compute-host-prune.timer'
    for a, b in [('G1', 'G2'), ('G2', 'G3'), ('G3', 'G4'), ('G3', 'G5'),
                 ('G4', 'G6'), ('G5', 'G6'), ('G6', 'G7'), ('G6', 'G8'),
                 ('G6', 'G9'), ('G9', 'G10'), ('G10', 'G11')]:
        assert max(e['at'] for e in notes(world, a)) < min(e['at'] for e in notes(world, b))
    assert max(e['at'] for e in notes(world, 'G5')) < min(at for at, _, _ in world.fileops('move'))
    for host in world.h.values():
        archive = Path(host.archive(world.profile.day))
        assert (archive / 'README.md').exists()
        manifest = archive / 'issue-automation-old/manifest.tsv'
        assert manifest.exists()
        assert len(manifest.read_text().splitlines()) == len(set(manifest.read_text().splitlines()))
        for line in manifest.read_text().splitlines():
            name, source, target, count, size = line.split('\t')
            assert name == host.name and not Path(source).exists()
            assert fsops.measure(Path(target)) == (int(count), int(size))
    for _, _, argv, _ in world.calls:
        assert argv[0] not in ('rm', 'rmdir', 'userdel')
        assert '-delete' not in argv
        assert argv[:2] not in (('docker', 'rm'), ('docker', 'rmi'))
    nft = [(at, host) for at, host, argv, _ in world.calls if argv[:4] == ('sudo', '-n', 'nft', 'list')]
    for at, host in nft:
        assert at < min(t for t, h, argv in world.stops() if h == host and 'issue-automation-sbx-nft.service' in argv)


def test_t141_held_admission_lock_prevents_all_stops_and_moves(world):
    h = world.h['local']
    lock = Path(h.home) / '.claude/issue-automation/locks/release.lock'
    with lock.open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        with pytest.raises(StepFailure):
            run_retirement(world.context(), world.profile)
    assert not world.stops()
    assert not world.fileops('move')
    assert 'retirement/' + h.name + '/admission' in world.cards


def test_t141_running_prune_waits_before_g3(world):
    compute_host = world.h['compute-host']
    world.busy_until[compute_host.name, 'issue-automation-compute-host-prune.service'] = world.t + 1.5
    result = run_retirement(world.context(), world.profile)
    assert result.steps['G2', compute_host.name] == 'ok'
    assert notes(world, 'G3')[0]['at'] > 1.5
    assert result.steps['G9', compute_host.name] == 'ok'


@pytest.mark.parametrize('role', ['local', 'legacy', 'compute-host'])
def test_t141_t143_timeout_is_host_local(world, role):
    h = world.h[role]
    unit = 'issue-automation-compute-host-prune.service' if role == 'compute-host' else 'issue-automation.service'
    world.busy_until[h.name, unit] = 1000
    result = run_retirement(world.context(), world.profile)
    assert result.steps['G2', h.name] == 'waiting'
    assert result.steps['G6', h.name] == 'skipped'
    assert all(r == 'ok' for (g, name), r in result.steps.items() if g == 'G11' and name != h.name)
    assert f'retirement/{h.name}/G2' in world.cards
    assert not any(host == h.name and unit in argv and 'disable' in argv for _, host, argv in world.stops())


def test_t143_t142_only_blocks_noded_payload(world):
    world.test_result('T-142', 'failed')
    result = run_retirement(world.context(), world.profile)
    assert result.steps['G9', world.h['compute-host'].name] == 'skipped'
    e = notes(world, 'G9')[0]
    assert e['data']['reason'] == 't142'
    assert Path(world.h['compute-host'].home, 'issue-automation-compute-host/bin/entry').exists()
    assert result.steps['G7', world.h['local'].name] == 'ok'
    assert result.steps['G8', world.h['legacy'].name] == 'ok'
    assert f"retirement/{world.h['compute-host'].name}/G9" in world.cards


@pytest.mark.parametrize('role', ['local', 'legacy', 'compute-host'])
def test_t143_unreachable_host_does_not_stop_others(world, role):
    h = world.h[role]
    world.unreachable.add(h.name)
    result = run_retirement(world.context(), world.profile)
    assert result.steps['G1', h.name] == 'failed'
    assert result.steps['G6', h.name] == 'skipped'
    assert all(r == 'ok' for (g, name), r in result.steps.items() if g == 'G11' and name != h.name)
    if role == 'compute-host':
        assert result.steps['G3', h.name] == 'failed'
        assert result.steps['G9', h.name] == 'skipped'
    assert not result.condition1


@pytest.mark.parametrize('role', ['local', 'legacy', 'compute-host'])
def test_t143_cross_filesystem_never_copies_and_only_blocks_that_host(world, role):
    h = world.h[role]
    world.cross_device.add(h.name)
    result = run_retirement(world.context(), world.profile)
    assert result.steps['G6', h.name] == 'failed'
    assert not any(host == h.name for _, host, _ in world.fileops('move'))
    assert all(r == 'ok' for (g, name), r in result.steps.items() if g == 'G11' and name != h.name)


def test_t143_prune_stop_failure_preserves_noded_storage(world):
    compute_host = world.h['compute-host']
    world.fail_disable.add((compute_host.name, 'user', 'issue-automation-compute-host-prune.service'))
    world.states[compute_host.name, 'user', 'issue-automation-compute-host-prune.service'][1] = 'enabled'
    result = run_retirement(world.context(), world.profile)
    assert result.steps['G3', compute_host.name] == 'failed'
    assert result.steps['G6', compute_host.name] == result.steps['G9', compute_host.name] == 'skipped'
    assert result.steps['G7', world.h['local'].name] == 'ok'


def test_t143_optional_umount_failure_does_not_block(world):
    world.fail_umount = True
    result = run_retirement(world.context(), replace(world.profile, optional_unmount=True))
    assert result.steps['G10', world.h['local'].name] == 'ok'
    assert result.steps['G11', world.h['local'].name] == 'ok'
    assert Path(world.h['local'].fstab).read_text().startswith('# ax-cutover: ')
    assert any(e['kind'] == 'removal.check' and e['data'].get('reason') == 'busy-left-mounted' for e in world.events)


@pytest.mark.parametrize('key', PRECONDITIONS)
def test_t144_prebuild_or_missing_prerequisite_has_zero_effects(world, key):
    pre = dict(world.profile.preconditions, **{key: False})
    with pytest.raises(StepFailure):
        run_retirement(world.context(), replace(world.profile, preconditions=pre))
    assert not world.calls
    for group in X_GROUPS:
        assert world.states[world.h['local'].name, 'user', group + '.timer'] == ['active', 'enabled']
        assert Path(world.h['local'].user_units, group + '.timer').exists()


def test_t144_x_units_retire_but_records_are_never_read(world):
    home = Path(world.h['local'].home)
    records = [home / '.claude/work/minecontext/x' / p for p in ('posted/id', 'queue/id', 'held/id', 'results.tsv')]
    records.append(home / '.local/state/x2discord/cursor')
    for p in records:
        world.file(p, 'do-not-import-this')
    result = run_retirement(world.context(), world.profile)
    assert result.condition1
    for group in X_GROUPS:
        assert world.states[world.h['local'].name, 'user', group + '.timer'] == ['inactive', 'disabled']
        assert Path(world.h['local'].archive(world.profile.day), 'units/user', group + '.timer').exists()
    assert all(p.read_text() == 'do-not-import-this' for p in records)
    assert not any(str(p) in world.file_reads for p in records)
    assert not any('do-not-import-this' in (stdin or '') for _, _, _, stdin in world.calls)


def test_t149_single_stop_set_and_archive_is_last(world):
    result = run_retirement(world.context(), world.profile)
    assert len(USER_GROUPS) == 28
    assert len(stop_set(world.profile)) == 35
    assert result.condition1 and result.next_step == 'check-condition3'
    stops = world.stops()
    archive_at = min(at for at, host, argv in stops if 'archive-to-hdd.timer' in argv)
    assert all(at < archive_at for at, _, argv in stops if any(a.endswith('.timer') and a != 'archive-to-hdd.timer' for a in argv))
    assert not any(host == world.h['legacy'].name and any(a.startswith('archive-to-hdd') for a in argv) for _, host, argv in stops)
    for group in ('magpie-gateway', 'agent-memory', 'paseo'):
        assert world.states[world.h['local'].name, 'user', group + '.service'][0] == 'active'


@pytest.mark.parametrize('result', ['failed', None])
def test_t149_failed_or_missing_t78_preserves_old_archiver(world, result):
    world.events = [e for e in world.events if e['subject'] != 'T-78']
    if result:
        world.test_result('T-78', result)
    r = run_retirement(world.context(), world.profile)
    h = world.h['local']
    assert not r.condition1 and not r.archive_to_hdd_stopped
    assert r.next_step == 'collect_only'
    assert world.states[h.name, 'user', 'archive-to-hdd.timer'] == ['active', 'enabled']
    assert Path(h.user_units, 'archive-to-hdd.timer').exists()
    assert r.steps['G7', h.name] == 'ok'
    assert 'retirement/t78' in world.cards
    assert len([k for k in world.cards if k == 'retirement/t78']) == 1


def test_t149_latest_failure_wins_and_later_ok_can_finish_once(world):
    world.test_result('T-78', 'failed')
    first = run_retirement(world.context(), world.profile)
    assert not first.condition1
    world.test_result('T-78', 'ok')
    second = run_retirement(world.context(), world.profile)
    assert second.condition1
    # Stage4 returns a condition, never invokes reap or enables external work.
    assert not any('reap' in a or a == 'set_mode' for _, _, argv, _ in world.calls for a in argv)
    for h in world.h.values():
        manifest = Path(h.archive(world.profile.day), 'issue-automation-old/manifest.tsv').read_text().splitlines()
        assert len(manifest) == len(set(manifest))


def test_t149_archiver_lock_waits_without_stopping_timer(world):
    h = world.h['local']
    world.held.add(h.name)
    # All gh locks must be clear at admission. Hold only the archive lock here.
    original = world.execute
    def execute(host, argv, stdin=None):
        if stdin:
            data = json.loads(stdin)
            if data['op'] == 'busy' and data.get('lock_patterns') == [str(Path(h.runtime_dir) / f'archive-to-hdd-{h.uid}.lock')]:
                return type(original(host, ('ps', '-eo', 'args')))(0, '{"busy":true,"items":["archive-lock"]}')
        held = set(world.held)
        world.held.clear()
        try:
            return original(host, argv, stdin)
        finally:
            world.held = held
    ctx = replace(world.context(), execute=execute)
    result = run_retirement(ctx, world.profile)
    assert not result.archive_to_hdd_stopped
    assert not any('archive-to-hdd.timer' in argv for _, _, argv in world.stops())
    assert 'retirement/' + h.name + '/archive-to-hdd' in world.cards


def test_go_or_lease_failure_cannot_execute(world):
    world.go = False
    with pytest.raises(PermissionError):
        run_retirement(world.context(), world.profile)
    assert world.calls == []


def test_destination_conflict_never_overwrites(world):
    h = world.h['local']
    dst = Path(h.archive(world.profile.day), 'issue-automation-old/state/issue-automation')
    dst.mkdir(parents=True)
    world.file(dst / 'precious', 'unchanged')
    result = run_retirement(world.context(), world.profile)
    assert result.steps['G6', h.name] == 'failed'
    assert (dst / 'precious').read_text() == 'unchanged'
    assert Path(h.home, '.claude/issue-automation').exists()


def test_fsops_recovers_interrupted_rename_without_duplicate_manifest(tmp_path):
    archive = tmp_path / 'archive'
    source, target = tmp_path / 'source', archive / 'issue-automation-old/state/source'
    source.write_text('payload')
    before = fsops.measure(source)
    fsops.handle({'op': 'prepare', 'archive': str(archive)})
    row = {'host': 'fake-host', 'source': str(source), 'target': str(target), 'files': before[0], 'bytes': before[1], 'at': '2030'}
    world_file = archive / 'issue-automation-old/move-intents.jsonl'
    world_file.parent.mkdir(exist_ok=True)
    world_file.write_text(json.dumps(row) + '\n')
    target.parent.mkdir(parents=True)
    source.rename(target)
    for _ in range(2):
        fsops.handle({'op': 'prepare', 'archive': str(archive)})
    assert len((archive / 'issue-automation-old/manifest.tsv').read_text().splitlines()) == 1


def test_fsops_busy_checks_real_flock_without_service(tmp_path):
    lock = tmp_path / 'held.lock'
    with lock.open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        assert fsops.handle({'op': 'busy', 'lock_patterns': [str(lock)]})['busy']
    assert not fsops.handle({'op': 'busy', 'lock_patterns': [str(lock)]})['busy']


def test_measure_counts_hardlinks_once_in_bytes_and_does_not_follow_symlinks(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    one, two, link = data / 'one', data / 'two', data / 'link'
    one.write_bytes(b'12345')
    os.link(one, two)
    link.symlink_to('one')
    count, size = fsops.measure(data)
    assert count == 2
    assert size == data.stat().st_size + one.stat().st_size + link.lstat().st_size


def test_t149_archiver_service_running_does_not_stop_timer(world):
    h = world.h['local']
    world.busy_until[h.name, 'archive-to-hdd.service'] = 1000
    result = run_retirement(world.context(), world.profile)
    assert not result.condition1
    assert not any('archive-to-hdd.timer' in argv for _, _, argv in world.stops())
    assert world.states[h.name, 'user', 'archive-to-hdd.timer'] == ['active', 'enabled']


def test_final_orphan_listener_prevents_condition1(world):
    original = world.execute
    def execute(host, argv, stdin=None):
        if tuple(argv) == ('ss', '-ltn'):
            return type(original(host, argv, stdin))(0, 'LISTEN 0 10 127.0.0.1:3450 0.0.0.0:*\n')
        return original(host, argv, stdin)
    result = run_retirement(replace(world.context(), execute=execute), world.profile)
    assert not result.condition1 and result.next_step == 'collect_only'
    assert 'retirement/' + world.h['local'].name + '/stop-set' in world.cards


def test_missing_unit_requires_confirmed_loadstate(world):
    original = world.execute
    def execute(host, argv, stdin=None):
        if 'is-enabled' in argv and argv[-1] == 'websbx-nft.timer':
            return type(original(host, ('ps', '-eo', 'args')))(1, '', 'No such file')
        return original(host, argv, stdin)
    result = run_retirement(replace(world.context(), execute=execute), world.profile)
    assert result.condition1
    assert any('LoadState' in argv for _, _, argv, _ in world.calls)


def test_fsops_exdev_preflight_has_no_partial_moves(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_text('do not copy')
    original = Path.lstat
    class FakeStat:
        st_dev = -1
    def fake_lstat(path):
        return FakeStat() if path == source else original(path)
    monkeypatch.setattr(Path, 'lstat', fake_lstat)
    with pytest.raises(OSError):
        fsops.handle({'op': 'preflight', 'archive': str(tmp_path / 'archive'),
                      'moves': [[str(source), str(target)]]})
    assert source.read_text() == 'do not copy' and not target.exists()


def test_local_work_inventory_does_not_expand_to_unlisted_prefixes(world):
    keep = Path(world.h['local'].home) / '.claude/work/issue-automation-unlisted/keep'
    world.file(keep, 'outside-inventory')
    result = run_retirement(world.context(), world.profile)
    assert result.condition1
    assert keep.read_text() == 'outside-inventory'


def test_missing_t142_proof_adapter_only_skips_noded_g9(world):
    def pending(host):
        raise NotImplementedError('T-142 verifier not connected')
    result = run_retirement(replace(world.context(), verify_noded_replacement=pending), world.profile)
    assert result.steps['G9', world.h['compute-host'].name] == 'skipped'
    assert result.steps['G7', world.h['local'].name] == 'ok'
    assert Path(world.h['compute-host'].home, 'issue-automation-compute-host/bin/entry').exists()


def test_context_default_noded_replacement_is_unverified(world):
    context = Context(world.execute, world.record, world.latest, world.card, world.authorize)
    assert context.verify_noded_replacement(world.h['compute-host']) is False

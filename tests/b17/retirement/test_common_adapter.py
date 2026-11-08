"""The common adapter invokes only injected fakes, including ax approval/claim."""
from dataclasses import replace
import json
import shlex
from pathlib import Path

import pytest

from axcommon.ids import new_ulid
from tools.cutover import common
from tools.cutover.retirement import StepFailure
from tools.cutover.retirement.common_adapter import (
    RetirementSession, common_executor, from_common,
)
from tests.fakes.b17.retirement.world import World


class FakeAuthority:
    def __init__(self):
        self.changes = []

    def verify(self, runner, changes=()):
        self.changes.append(tuple(changes))
        return {}


class FakeAx:
    def __init__(self, world):
        self.world = world
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        if list(argv[:4]) == ['ax', 'claim', 'renew', 'cutover']:
            data = {'holder': True, 'claim_event_id': new_ulid(),
                    'owner': {'principal': 'seat-test', 'session': 'test-session', 'via': 'local'}}
            return common.CommandResult(0, json.dumps({'ok': True, 'data': data}))
        if len(argv) > 3 and str(argv[2]).endswith('cutover_note.py'):
            kind, subject, target, result, reason = argv[3:]
            data = {'target' if kind == 'cutover.state' else 'component_id': target, 'result': result}
            if reason:
                data['reason'] = reason
            self.world.record(kind, subject, data)
            return common.CommandResult(0, new_ulid() + '\n')
        raise AssertionError('Unexpected real command in common adapter test')


def test_common_context_claim_scope_and_note_are_used(tmp_path, monkeypatch):
    w = World(tmp_path)
    authority = FakeAuthority()
    runner = FakeAx(w)
    profile = common.Profile(w.h['local'].name, tmp_path / 'ax-state', tmp_path / 'tmp')
    ctx = common.Context(profile, Path(w.h['local'].archive(w.profile.day)), runner,
                         dry_run=False, authority=authority)
    monkeypatch.setattr(ctx, 'records', lambda: [dict(e, ts=e['at'], id=f"{i:026}") for i, e in enumerate(w.events)])
    ports = from_common(ctx, w.profile, execute=w.execute, card=w.card,
                        verify_noded_replacement=w.context().verify_noded_replacement,
                        monotonic=lambda: w.t, sleep=w.sleep,
                        utc=lambda: f'2030-01-02T00:00:{w.t:09.6f}+00:00')
    session = RetirementSession.create(ports, w.profile)
    others = session.retire_others()
    assert not others.archive_to_hdd_stopped and not others.condition1
    assert not any('archive-to-hdd.timer' in argv for _, _, argv in w.stops())
    final = session.retire_archive_to_hdd()
    assert final.condition1 and final.next_step == 'check-condition3'
    assert (7,) in authority.changes and (12,) in authority.changes
    assert any(call[3] == 'removal.check' for call in runner.calls if len(call) > 3 and str(call[2]).endswith('cutover_note.py'))
    assert all(n.get('planned') is None for n in ctx.notes)


def test_common_dry_run_never_reaches_host(tmp_path):
    w = World(tmp_path)
    ctx = common.Context(common.Profile('fake-host', tmp_path / 'state', tmp_path / 'tmp'), tmp_path / 'archive')
    ports = from_common(ctx, w.profile, execute=w.execute, card=w.card,
                        verify_noded_replacement=w.context().verify_noded_replacement)
    with pytest.raises(common.GateBlocked):
        RetirementSession.create(ports, w.profile).retire_others()
    assert not w.calls


def test_phase_order_cannot_stop_archiver_first(tmp_path):
    w = World(tmp_path)
    with pytest.raises(StepFailure):
        RetirementSession.create(w.context(), w.profile).retire_archive_to_hdd()
    assert not w.calls


def test_runner_bridge_quotes_payload_and_host_routes(tmp_path):
    w = World(tmp_path)
    class Capture:
        def __init__(self):
            self.calls = []
        def guard(self, changes=()):
            pass
        def run(self, argv, **kwargs):
            self.calls.append(tuple(argv))
            return common.CommandResult(0, '{}')
    ctx = Capture()
    execute = common_executor(ctx, w.profile, {
        w.h['local'].name: (), w.h['compute-host'].name: ('ssh', 'compute-host-route-from-profile'),
    })
    text = json.dumps({'path': "file with 'quotes' $(do-not-run) `do-not-run`"})
    execute(w.h['compute-host'].name, ('python3', '-I', '-c', 'print(input())'), text)
    route = ctx.calls[0]
    assert route[:2] == ('ssh', 'compute-host-route-from-profile')
    argv = shlex.split(route[2])
    assert argv[:3] == ['python3', '-I', '-c']
    compile(argv[3], '<bridge-test>', 'exec')
    assert 'io.StringIO' in argv[3]
    execute(w.h['local'].name, ('ps', '-eo', 'args'))
    assert ctx.calls[1] == ('ps', '-eo', 'args')
    with pytest.raises(StepFailure):
        execute('unregistered-host', ('ps',))

"""T-32 and T-33: permission, dry-run and exact-SHA publication checks."""
from __future__ import annotations

from tests.b15.conftest import SHA


def plan(service, batch):
    return service.plan(batch['batch_id'], latest_tag='v1.2.3', changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}],
                        diff_ref='diff:1', fragment_refs=['fragment:1'])


def test_t32_dry_run_keeps_pending_items_and_never_submits(service, dream_batch, ports):
    plan(service, dream_batch)
    result = service.execute(dream_batch['batch_id'])
    assert result['outcome'] == 'skipped'
    assert result['reason'] == 'dry_run'
    assert service.store.item('example-owner/shape-of-dreams-rpg-plan', 1)['state'] == 'pending'
    assert not any(call[0] == 'publish' for call in ports.calls)


def test_t32_denied_permission_holds_only_this_batch(service, dream_batch, ports):
    plan(service, dream_batch)
    ports.allowed = False
    result = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert result['outcome'] == 'held'
    assert result['reason'] == 'permission_denied'
    assert not any(call[0] == 'publish' for call in ports.calls)


def test_t33_requires_clean_main_matching_sha_evidence_and_fast_forward(service, dream_batch, ports):
    plan(service, dream_batch)
    ports.snapshot_data['clean'] = False
    result = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert result['reason'] == 'worktree_not_clean_main'
    ports.snapshot_data['clean'] = True
    ports.snapshot_data['dev_sha'] = 'b' * 40
    result = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert result['reason'] == 'dev_head_changed_revalidate'


def test_t33_success_binds_all_evidence_to_frozen_sha(service, dream_batch, ports):
    plan(service, dream_batch)
    result = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert result['outcome'] == 'success'
    assert result['target_sha'] == SHA
    assert any(call[0] == 'publish' for call in ports.calls)

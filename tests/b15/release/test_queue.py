"""T-30 and T-31: queue identity, immutable batches and terminal outcomes."""
from __future__ import annotations

from tests.b15.conftest import SHA, merged


def test_t30_keeps_duplicate_and_later_merge_for_next_batch(service):
    first = service.ingest(**merged())
    assert service.ingest(**merged()) == first
    batch = service.store.form_batch(service.registry.get('dreamforge'), SHA, force=True)
    assert batch['target_sha'] == SHA
    later = merged(pr=2)
    later['merge_sha'] = 'b' * 40
    service.ingest(**later)
    assert [item['pr'] for item in service.store.items() if item['state'] == 'pending'] == [2]
    assert service.store.batch(batch['batch_id'])['prs'] == [1]


def test_t30_serializes_same_repo_mode_and_holds_at_wait_limit(service, clock):
    service.ingest(**merged())
    first = service.store.form_batch(service.registry.get('dreamforge'), SHA, force=True)
    assert service.store.acquire(first['batch_id']) is True
    service.ingest(**merged(pr=2))
    second = service.store.form_batch(service.registry.get('dreamforge'), SHA, force=True)
    assert service.store.acquire(second['batch_id'], wait_min=1) is False
    clock[0] = '2026-11-07T12:02:00.000Z'
    assert service.store.acquire(second['batch_id'], wait_min=1) is False
    assert service.store.batch(second['batch_id'])['state'] == 'held'


def test_t31_records_skipped_disabled_and_preserves_diagnostic_issue(service):
    skipped = service.ingest(**merged(paths=['README.md']))
    assert skipped['state'] == 'skipped'
    product = service.registry.get('dreamforge')
    product['enabled'] = False
    disabled = service.store.ingest(product, **merged(pr=2))
    assert disabled['state'] == 'disabled'
    event = merged(pr=3)
    event['changed_paths'] = None
    service.ingest(**event)
    held = service.store.item('example-owner/shape-of-dreams-rpg-plan', 3)
    assert held['state'] == 'held'


def test_issue_repository_survives_restart_and_duplicate_ingest(tmp_path, products, ports):
    from plugins.subloop.release.core.service import ReleaseService
    from plugins.subloop.release.core.store import Store

    path = tmp_path / 'release.db'
    store = Store(path)
    service = ReleaseService(products, store, ports)
    event = merged(paths=['README.md'])
    event['issues'] = [{'number': 10, 'issue_repo': 'public/issues'}, {'number': 10}]
    first = service.ingest(**event)
    assert first['issues'][0]['issue_repo'] == 'public/issues'
    assert store.get(f"issue:{event['repo']}:1:10:skipped:public/issues")['repo'] == 'public/issues'
    assert store.get(f"issue:{event['repo']}:1:10:skipped")['repo'] == event['repo']
    store.close()

    store = Store(path)
    service = ReleaseService(products, store, ports)
    assert store.item(event['repo'], 1) == first
    assert service.ingest(**event) == first
    assert len(store.items()) == 1
    assert store.get(f"issue:{event['repo']}:1:10:skipped:public/issues")['repo'] == 'public/issues'
    store.close()


def test_legacy_issue_record_survives_restart(tmp_path, products, ports):
    from plugins.subloop.release.core.service import ReleaseService
    from plugins.subloop.release.core.store import Store

    path = tmp_path / 'release.db'
    store = Store(path)
    service = ReleaseService(products, store, ports)
    event = merged(paths=['README.md'])
    first = service.ingest(**event)
    assert 'issue_repo' not in first['issues'][0]
    store.close()
    store = Store(path)
    service = ReleaseService(products, store, ports)
    assert service.ingest(**event) == first
    assert store.get(f"issue:{event['repo']}:1:10:skipped")['repo'] == event['repo']
    store.close()


def test_invalid_issue_repository_is_rejected(service):
    import pytest
    from plugins.subloop.release.core.registry import Held

    event = merged()
    event['issues'] = [{'number': 10, 'issue_repo': 'https://example.invalid/repo'}]
    with pytest.raises(Held, match='invalid_issue_repo'):
        service.ingest(**event)
    assert service.store.item(event['repo'], 1) is None

"""T-34, T-89 and T-95: product-specific rules and environment hand-off."""
from __future__ import annotations

from plugins.subloop.release.core.registry import Held, next_version
from plugins.subloop.release.publish.env import spec
from plugins.subloop.release.publish.hibi import assets_ready
from tests.b15.conftest import SHA, merged


def test_t34_dreamforge_normal_edition_and_receipt_protect_workshop_fields(service, dream_batch, ports):
    publication = service.plan(dream_batch['batch_id'], latest_tag='v1.2.3',
                               changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}],
                               diff_ref='diff:1')
    assert publication['edition'] == 'normal'
    assert publication['workshop_fields'] == ['content', 'changenote']
    result = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert result['outcome'] == 'success'
    assert not any(value in publication['workshop_fields'] for value in ('title', 'description', 'preview'))


def test_t89_env_run_uses_required_capability_and_target_sha(products):
    product = products.get('dreamforge')
    job = spec(product, {'batch_id': '01JABC', 'target_sha': SHA},
               {'version': 'v1.2.4', 'notes': {'languages': {'ja': 'ja', 'en': 'en'}}})
    assert job['requires'] == ['steam-workshop-upload']
    assert job['entry']['name'] == 'release-both'
    assert job['entry']['args']['target_sha'] == SHA
    assert job['irreversible'] is True


def test_t89_environment_unavailable_parks_only_the_publication(service, dream_batch, ports):
    service.plan(dream_batch['batch_id'], latest_tag='v1.2.3', changes=[{'kind': 'fix', 'text': {'ja': '修正', 'en': 'Fix'}}], diff_ref='diff:1')
    original = ports.publish
    ports.publish = lambda *args: {'outcome': 'parked'}
    result = service.execute(dream_batch['batch_id'], explicit={'dry_run': False})
    assert result['outcome'] == 'held'
    assert result['reason'] == 'environment_unavailable'
    ports.publish = original


def test_t95_registry_owns_version_rules_and_rejects_unregistered_products(products, service):
    assert set(products.products) == {'dreamforge', 'hibi'}
    assert next_version(products.get('hibi'), 'v0.1.0-dev.15', [{'kind': 'fix'}]) == 'v0.1.0-dev.16'
    assert assets_ready([{'name': 'AX.dmg'}, {'name': 'setup.exe'}, {'name': 'latest.yml'}])
    assert not assets_ready([{'name': 'AX.dmg'}])
    try:
        products.get('agent-memory')
    except Held as exc:
        assert str(exc) == 'product_unregistered'
    else:
        raise AssertionError('agent-memory must not become a deployment target')


def test_t95_hibi_is_held_until_its_environment_entry_is_defined(service):
    service.ingest(**merged(repo='example-owner/hibi', paths=['app/main.ts']))
    batch = service.store.form_batch(service.registry.get('hibi'), SHA, force=True)
    service.plan(batch['batch_id'], latest_tag='v0.1.0-dev.15', changes=[{'kind': 'fix', 'text': {'en': 'Fix'}}], diff_ref='diff:hibi')
    result = service.execute(batch['batch_id'], explicit={'dry_run': False})
    assert result['outcome'] == 'held'
    assert result['reason'] == 'publication_contract_undefined'

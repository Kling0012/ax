from __future__ import annotations

from pathlib import Path

import pytest

from plugins.subloop.release.core.registry import ProductRegistry
from plugins.subloop.release.core.service import ReleaseService
from plugins.subloop.release.core.store import Store
from tests.fakes.b15.release.ports import FakeReleasePorts, SHA


@pytest.fixture
def products(repo_root):
    registry = ProductRegistry.load(repo_root / 'config' / 'products.yaml')
    for product in registry.products.values():
        product['repo'] = 'example-owner/' + product['repo'].split('/', 1)[1]
    return registry


@pytest.fixture
def clock():
    value = ['2026-11-07T12:00:00.000Z']
    return value


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / 'release.db', clock=lambda: clock[0])


@pytest.fixture
def ports():
    return FakeReleasePorts()


@pytest.fixture
def service(products, store, ports):
    return ReleaseService(products, store, ports)


def merged(*, repo='example-owner/shape-of-dreams-rpg-plan', pr=1, paths=None):
    return {'repo': repo, 'pr': pr, 'merge_sha': SHA, 'merged_at': '2026-11-07T11:00:00.000Z',
            'issues': [{'number': 10}], 'changed_paths': paths or ['src/example.cs']}


@pytest.fixture
def dream_batch(service):
    service.ingest(**merged())
    batch = service.store.form_batch(service.registry.get('dreamforge'), SHA, force=True)
    return batch

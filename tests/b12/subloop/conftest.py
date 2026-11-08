from pathlib import Path

import pytest

from tests.fakes.b12.subloop.world import World


@pytest.fixture
def world(tmp_path, repo_root):
    world = World(tmp_path, repo_root)
    yield world
    world.store.close()

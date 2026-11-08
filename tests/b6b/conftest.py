import pytest

from tests.b6b.helpers import Sandbox


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)

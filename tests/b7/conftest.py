"""B7 の試験の fixture。env: 偽の部品で組んだ Services と Executor の工場。"""
import pytest

from tests.fakes.b7 import make_env


@pytest.fixture
def env(tmp_path, repo_root, fake_clock):
    return make_env(tmp_path, repo_root, fake_clock)

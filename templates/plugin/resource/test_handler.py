"""Copy to a bundle test directory after placing the resource at its final path."""
from __future__ import annotations

import importlib.util

import yaml
from starlette.testclient import TestClient

from axcommon import schemas


def _load(repo_root):
    path = repo_root / "plugins/resource/example-resource/handler.py"
    spec = importlib.util.spec_from_file_location("example_resource", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_resource_manifest_matches_schema(repo_root):
    path = repo_root / "plugins/resource/example-resource/manifest.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert schemas.errors("manifest", manifest) == []


def test_example_resource_exposes_status(repo_root):
    module = _load(repo_root)

    with TestClient(module.build_app()) as client:
        response = client.get("/status")

    assert response.status_code == 200
    assert response.json()["mode"] == "run"

"""Register runtime and plugin manifests at control startup through its public API."""
from pathlib import Path

from starlette.testclient import TestClient
import yaml

from core.control.app import create_core_app

SOURCE = Path(__file__).resolve().parents[2]


def test_runtime_manifest_registration_and_path_boundaries(control_env, tmp_path):
    env = control_env
    root = env["root"]
    template = yaml.safe_load((SOURCE / "runtime/timer_runner/manifest.yaml").read_text())
    runtime = root / "runtime"

    def manifest(path, identifier):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(template, id=identifier)
        path.write_text(yaml.safe_dump(data))

    manifest(runtime / "timer_runner/manifest.yaml", "timer-runner")
    manifest(runtime / "another_component/manifest.yaml", "another-runtime")
    manifest(runtime / "wrong_name/arbitrary.yaml", "wrong-name")
    outside = tmp_path / "outside/manifest.yaml"
    manifest(outside, "outside-file")
    (runtime / "escape").mkdir()
    (runtime / "escape/manifest.yaml").symlink_to(outside)
    (runtime / "other_component").symlink_to(runtime / "another_component", target_is_directory=True)
    plugin_link = root / "plugins/timer/escape/manifest.yaml"
    plugin_link.parent.mkdir(parents=True)
    plugin_link.symlink_to(runtime / "timer_runner/manifest.yaml")
    manifest(runtime / "bad_id/manifest.yaml", "different-id")
    manifest(runtime / "bad_kind/manifest.yaml", "bad-kind")
    (runtime / "bad_schema").mkdir()
    (runtime / "bad_schema/manifest.yaml").write_text("id: bad-schema\nkind: timer\n")
    entries = [
        {"id": "timer-runner", "kind": "timer", "path": "runtime/timer_runner/manifest.yaml"},
        {"id": "another-runtime", "kind": "timer", "path": "runtime/another_component/manifest.yaml"},
        {"id": "worker-pool", "kind": "resource", "path": "plugins/resource/worker-pool/manifest.yaml"},
        {"id": "wrong-name", "kind": "timer", "path": "runtime/wrong_name/arbitrary.yaml"},
        {"id": "outside-file", "kind": "timer", "path": str(outside)},
        {"id": "traversal", "kind": "timer", "path": "runtime/../outside/manifest.yaml"},
        {"id": "bad-schema", "kind": "timer", "path": "runtime/bad_schema/manifest.yaml"},
        {"id": "symlink-escape", "kind": "timer", "path": "runtime/escape/manifest.yaml"},
        {"id": "symlink-sibling", "kind": "timer", "path": "runtime/other_component/manifest.yaml"},
        {"id": "plugin-escape", "kind": "timer", "path": "plugins/timer/escape/manifest.yaml"},
        {"id": "bad-id", "kind": "timer", "path": "runtime/bad_id/manifest.yaml"},
        {"id": "bad-kind", "kind": "resource", "path": "runtime/bad_kind/manifest.yaml"},
    ]
    (root / "plugins/registry.yaml").write_text(yaml.safe_dump({"schema_version": 1, "plugins": entries}))
    with TestClient(create_core_app(env["service"])) as client:
        views = {entry["id"]: client.get('/v1/plugins/' + entry["id"]).json() for entry in entries}
    for name in ("timer-runner", "another-runtime", "worker-pool"):
        assert views[name]["status"] == "registered", views[name]
    for name in ("wrong-name", "outside-file", "traversal", "bad-schema", "symlink-escape", "symlink-sibling", "plugin-escape", "bad-id", "bad-kind"):
        assert views[name]["status"] == "rejected", views[name]

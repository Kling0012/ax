"""Keep performance unregistered until its runtime integration is reviewed."""
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
REFERENCES = ("plugins/subloop/performance", "core/intake/dest/z_performance")


def _strings(value):
    if isinstance(value, str):
        yield value.replace("\\", "/").replace(".", "/")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _performance(entry, manifest):
    references = tuple(ref.replace(".", "/") for ref in REFERENCES)
    if any(ref in value for value in _strings((entry, manifest)) for ref in references):
        return True
    for field in ("kind", "type", "capabilities", "capability"):
        if any("performance" in value.lower() for value in _strings(manifest.get(field))):
            return True
    return False


def _registered(root):
    registry = root / "plugins/registry.yaml"
    fragments = root / "plugins/registry.d"
    assert registry.is_file() and fragments.is_dir()
    entries = []
    for path in [registry, *sorted(fragments.glob("*.yaml")), *sorted(fragments.glob("*.yml"))]:
        document = yaml.safe_load(path.read_text())
        assert isinstance(document, dict) and isinstance(document.get("plugins"), list)
        for entry in document["plugins"]:
            manifest_path = root / entry["path"]
            assert manifest_path.is_file(), f"missing manifest: {manifest_path}"
            manifest = yaml.safe_load(manifest_path.read_text())
            assert isinstance(manifest, dict)
            if _performance(entry, manifest):
                entries.append((path.name, entry["id"]))
    return entries


def test_no_registered_performance_entry():
    assert _registered(ROOT) == []


@pytest.mark.parametrize("manifest", [
    {"kind": "performance"},
    {"capabilities": ["performance"]},
    {"implementation": "plugins.subloop.performance"},
    {"handler": "core/intake/dest/z_performance"},
])
@pytest.mark.parametrize("source", ["registry.yaml", "registry.d/example.yaml"])
def test_detects_new_performance_registration(tmp_path, source, manifest):
    plugins = tmp_path / "plugins"
    (plugins / "registry.d").mkdir(parents=True)
    (plugins / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (plugins / "registry.yaml").write_text("plugins: []\n")
    entry = {"plugins": [{"id": "example", "kind": "subloop", "path": "plugins/manifest.yaml"}]}
    (plugins / source).write_text(yaml.safe_dump(entry))
    assert _registered(tmp_path) == [(Path(source).name, "example")]


def test_scheduled_registered_fixture_uses_stopped_gate(tmp_path):
    import asyncio
    from axcommon import timeutil
    from plugins.subloop.performance import Performance
    from runtime.subloop.store import Store
    from runtime.timer_runner.runner import TimerRunner, load_handler

    class Backend:
        def __init__(self):
            self.started, self.prs = [], []
        def prepare(self, target, item):
            self.started.append((target, item))
            return {"id": "fixture-change"}
        def create_pr(self, target, item, change):
            self.prs.append((target, item))
            return "fixture-pr"

    class Sink:
        def emit(self, *args, **kwargs):
            pass

    plugin = tmp_path / "plugins/timer/example"
    plugin.mkdir(parents=True)
    manifest = {"id": "example", "kind": "timer", "capabilities": ["performance"],
                "schedule": {"kind": "interval", "every_s": 60}}
    (plugin / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (plugin / "handler.py").write_text(
        'async def run(ctx):\n'
        '    return ctx.clients["performance"].run("repo-a", "item", trigger="schedule")\n')
    registry = tmp_path / "plugins/registry.yaml"
    registry.write_text(yaml.safe_dump({"plugins": [{"id": "example", "kind": "timer",
        "path": "plugins/timer/example/manifest.yaml"}]}))
    (tmp_path / "plugins/registry.d").mkdir()
    assert _registered(tmp_path) == [("registry.yaml", "example")]
    registration = yaml.safe_load(registry.read_text())["plugins"][0]
    manifest = yaml.safe_load((tmp_path / registration["path"]).read_text())
    backend = Backend()
    perf = Performance(str(tmp_path / "gate.db"), backend)
    store = Store(tmp_path / "runner.db")
    runner = TimerRunner("timer-runner", store, Sink(), {"example": load_handler(plugin)},
                         {"example": manifest}, lambda *_: None,
                         extra_clients={"example": {"performance": perf}})

    async def go():
        await runner.run_job("example", timeutil.now_ts())
        assert backend.started == [] and backend.prs == []
        assert not perf.resume("repo-a", issuer="emergency", explicit=True)
        await runner.run_job("example", timeutil.now_ts())
        assert backend.started == [] and backend.prs == []
        assert perf.resume("repo-a", issuer="user", explicit=True)
        await runner.run_job("example", timeutil.now_ts())
        assert backend.started == [("repo-a", "item")]
        assert backend.prs == [("repo-a", "item")]

    try:
        asyncio.run(go())
    finally:
        store.close()
        perf.db.close()


def _assert_performance_pair(doc):
    variables = {"AX_PERFORMANCE_DB", "AX_PERFORMANCE_TARGETS"}
    for service in (doc.get("services") or {}).values():
        environment = service.get("environment") or {}
        keys = set(environment if isinstance(environment, dict) else
                   (item.split("=", 1)[0] for item in environment))
        assert not keys.intersection(variables) or variables <= keys


@pytest.mark.parametrize("key", ["AX_PERFORMANCE_DB", "AX_PERFORMANCE_TARGETS"])
def test_compose_rejects_single_variable_fragment(key):
    with pytest.raises(AssertionError):
        _assert_performance_pair({"services": {"fixture": {"environment": {key: ""}}}})


def test_compose_performance_variables_are_paired():
    import json
    import os
    import shutil
    import subprocess

    if not shutil.which("docker"):
        pytest.skip("docker compose CLI unavailable")
    variables = {"AX_PERFORMANCE_DB", "AX_PERFORMANCE_TARGETS"}
    for fragment in sorted((ROOT / "compose.d").glob("*.yaml")):
        doc = yaml.safe_load(fragment.read_text()) or {}
        _assert_performance_pair(doc)
    env = dict(os.environ, DOCKER_HOST="unix:///nonexistent")
    for key in variables:
        env.pop(key, None)
    result = subprocess.run(["docker", "compose", "-p", "ax-t69-test", "--project-directory", str(ROOT),
        "--env-file", str(ROOT / "tools/ci-fixtures/ci.env"), "-f", str(ROOT / "compose.yaml"),
        "-f", str(ROOT / "compose.d/b5.yaml"), "config", "--format", "json"],
        capture_output=True, text=True, env=env, check=True)
    environment = json.loads(result.stdout)["services"]["intake"]["environment"]
    assert variables <= environment.keys()
    assert not environment["AX_PERFORMANCE_DB"] and not environment["AX_PERFORMANCE_TARGETS"]


def test_subloop_apply_run_now_obeys_performance_gate(tmp_path):
    import asyncio
    import httpx
    from plugins.subloop.performance import Performance
    from runtime.subloop.app import build_app
    from runtime.subloop.nodeapi import merge_meta, ok
    from runtime.subloop.nodes import NodeType
    from tests.b7.exhelp import graph, manifest as make_manifest, node
    from tests.fakes.b7 import make_env

    plugin = tmp_path / "plugins/subloop/example"
    plugin.mkdir(parents=True)
    manifest = make_manifest(id="example", capabilities=["run", "performance"], schedule={"kind": "manual"})
    (plugin / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    registry = tmp_path / "plugins/registry.yaml"
    registry.write_text(yaml.safe_dump({"plugins": [{"id": "example", "kind": "subloop",
        "path": "plugins/subloop/example/manifest.yaml"}]}))
    (tmp_path / "plugins/registry.d").mkdir()
    assert _registered(tmp_path) == [("registry.yaml", "example")]
    entry = yaml.safe_load(registry.read_text())["plugins"][0]
    manifest = yaml.safe_load((tmp_path / entry["path"]).read_text())

    class Backend:
        def __init__(self):
            self.started, self.prs = [], []
        def prepare(self, target, item):
            self.started.append((target, item))
            return {"id": "fixture-change"}
        def create_pr(self, target, item, change):
            self.prs.append((target, item))
            return "fixture-pr"

    backend = Backend()
    perf = Performance(str(tmp_path / "performance.db"), backend)
    env = make_env(tmp_path, ROOT)
    ex = env.executor(graph([node("perform", "fixture.performance")], []), manifest)
    calls = []
    async def fixture_handler(ctx, inp):
        calls.append("handler")
        return ok(perf.run("repo-a", "item", trigger="run_now"))
    ex.registry.types["fixture.performance"] = NodeType("fixture.performance", "fixture", {},
                                                         fixture_handler, None, merge_meta(None))
    app = build_app(ex, apply_token="test-token", run_loops=False)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async def apply(revision):
                response = await client.post("/apply", headers={"Authorization": "Bearer test-token"},
                                             json={"mode": "run", "revision": revision, "run_now": True})
                assert response.status_code == 200 and response.json()["applied"]
                await ex.tick()
                await ex.drain()
            await apply(1)
            assert len(calls) == 1 and backend.started == [] and backend.prs == []
            assert not perf.resume("repo-a", issuer="emergency", explicit=True)
            await apply(2)
            assert len(calls) == 2 and backend.started == [] and backend.prs == []
            assert perf.resume("repo-a", issuer="user", explicit=True)
            await apply(3)
            assert len(calls) == 3 and backend.started == [("repo-a", "item")]
            assert backend.prs == [("repo-a", "item")]
    try:
        asyncio.run(go())
    finally:
        env.store.close()
        perf.db.close()


def test_control_paths_have_no_performance_implementation_reference():
    import re
    paths = ("runtime/timer_runner", "runtime/subloop", "core/control", "core/manager",
             "core/inspector", "core/intake/dest/b10a")
    patterns = (
        r"plugins[./]subloop[./]performance",
        r"core[./]intake[./]dest[./]z_performance",
        r"from\s+plugins\.subloop\s+import\s+[^\n]*\bperformance\b",
        r"from\s+core\.intake\.dest\s+import\s+[^\n]*\bz_performance\b",
    )
    matches = []
    for directory in paths:
        files = list((ROOT / directory).rglob("*.py"))
        assert files, directory
        for path in files:
            if any(re.search(pattern, path.read_text(encoding="utf-8")) for pattern in patterns):
                matches.append(str(path.relative_to(ROOT)))
    assert matches == []
    assert _registered(ROOT) == []

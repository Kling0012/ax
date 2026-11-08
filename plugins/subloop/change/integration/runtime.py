"""Assembly for b7. No network, listener or process starts during construction."""
from __future__ import annotations

import os
from pathlib import Path

from .ports import DependencyUnavailable, Ports, load_factory
from .flow import ChangeFlow
from .journal import Journal
from .register import register


def build_executor(*, root: Path, services=None, ports: Ports | None = None, environ=None):
    from axcommon.redact import from_environment
    from axcommon.timeutil import now
    from runtime.subloop.config import build_services
    from runtime.subloop.executor import Executor
    from runtime.subloop.graph import Graph, expand, load_yaml
    from runtime.subloop.graphcheck import check_graph
    from runtime.subloop.nodes import load_registry
    from runtime.subloop.store import Store

    env = dict(os.environ if environ is None else environ)
    plugin_dir = root / "plugins" / "subloop" / "change"
    manifest = load_yaml(plugin_dir / "manifest.yaml")
    svc = services or build_services(environ=env, manifest=manifest, repo_root=root)
    data = Path(env.get("AX_DATA_DIR", "/data"))
    data.mkdir(parents=True, exist_ok=True)
    redactor = from_environment(env)
    scrub = lambda value: redactor.redact_obj(value)[0]
    journal = Journal(data / "change.db", scrub=scrub, host=env.get("AX_HOST", "unknown"))
    targets_path = Path(env.get("AX_CHANGE_TARGETS_FILE") or plugin_dir / "targets.yaml")
    ledger_path = env.get("AX_CHANGE_LEDGER_PATH")
    if ledger_path:
        from core.intake.dest.b12b3.change import ChangeLedger
        doc_targets = load_yaml(targets_path)
        journal.change_ledger = ChangeLedger(ledger_path, [t["issue_repo"] for t in doc_targets["targets"]])
    errors = []
    if ports is None:
        try:
            ports = load_factory(env.get("AX_CHANGE_PORT_FACTORY", ""), services=svc, journal=journal)
        except Exception:
            errors.append({"code": "change_ports_unconfigured", "severity": "error", "message": "trusted change adapters are required"})
    flow = None
    if ports is not None:
        # Registered targets are the only source of mappings and policies.
        doc_targets = load_yaml(targets_path)
        flow = ChangeFlow(ports, journal, doc_targets["targets"], clock=lambda: now().timestamp(),
                          fix_rounds=int(svc.defaults.get("change_fix_rounds")))
    registry = register(load_registry(root / "plugins" / "nodes"), flow)
    doc = load_yaml(plugin_dir / "graph.yaml")
    findings = check_graph(doc, manifest, lib_dir=svc.lib_dir, registry=registry, defaults=svc.defaults)
    errors.extend(f.as_dict() for f in findings if f.severity == "error")
    graph = expand(doc, svc.lib_dir) if not errors else Graph(entry="none", item_source=None, max_parallel_items=1, nodes={}, edges=[])
    store = Store(data / "subloop.db")
    if store.get_meta("mode") is None:
        store.set_meta("mode", "collect_only")
    ex = Executor(svc=svc, graph=graph, manifest=manifest, registry=registry, store=store)
    # Preserve the outbox even when adapters are not yet available.
    ex.change_journal = journal
    return ex, flow, errors

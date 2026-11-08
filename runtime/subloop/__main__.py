"""サブループの起動。manifest・graph を読み、検査し、実行系と HTTP を立てる（D-6.1）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path



class XInventoryBinding:
    """Inventory targets; keep delivery disabled until a sender is connected."""

    async def collect(self, ctx, *, phase="queue"):
        from core.intake.dest.b16b.common import TargetRegistry
        from runtime.subloop.nodeapi import error, ok
        try:
            view = await ctx.call(ctx.svc.control.get, "/v1/plugins/x-posts")
            targets = TargetRegistry(view["delivery_targets"]).for_plugin("x-posts")
            ctx.svc.emit("automation.targets", "x-posts", {"targets": list(targets)})
        except Exception:
            return error("x_targets_unavailable", "X の送信対象を取得できない")
        if ctx.mode == "run" and phase == "queue":
            from axcommon.client import ApiError
            todo = {"id": "x-posts-sender-unavailable", "title": "X 送信器を接続する",
                    "state": "todo", "owner": "system", "subloop": "x-posts"}
            try:
                await ctx.call(ctx.svc.ledger.create_todo, todo)
            except ApiError as exc:
                if exc.status != 409:
                    return error("x_sender_todo_unavailable", "X 送信器の未接続を記録できない")
            except Exception:
                return error("x_sender_todo_unavailable", "X 送信器の未接続を記録できない")
        return ok({"items": [], "reason": "sender_unavailable" if ctx.mode == "run" else "collect_only"})

    async def draft(self, ctx):
        from runtime.subloop.nodeapi import error
        return error("x_sender_unavailable", "X の処理は未接続")

    async def send(self, ctx):
        from runtime.subloop.nodeapi import error
        return error("x_sender_unavailable", "X の送信器は未接続")

    async def reconcile(self, ctx, key):
        return None


def main() -> int:
    import uvicorn

    from axcommon import schemas  # noqa: F401
    from . import graphcheck
    from .app import build_app
    from .config import build_services
    from .executor import Executor
    from .graph import expand, load_yaml
    from .nodes import subloop_registry
    from .store import Store

    pid = os.environ["AX_PLUGIN_ID"]
    root = Path(os.environ.get("AX_HOME_IN_CONTAINER", "/ax"))
    plugin_dir = Path(os.environ.get("AX_PLUGIN_DIR") or root / "plugins" / "subloop" / pid)
    manifest = load_yaml(plugin_dir / ("draft.manifest.yaml" if pid == "x-posts-draft" else "manifest.yaml"))
    svc = build_services(manifest=manifest, repo_root=root)
    if pid in ("x-posts", "x-posts-draft"):
        svc.x_posts_binding = XInventoryBinding()
    registry = subloop_registry(root, plugin_dir)
    doc = load_yaml(plugin_dir / manifest.get("graph", "graph.yaml"))
    findings = graphcheck.check_graph(doc, manifest, lib_dir=svc.lib_dir, registry=registry, defaults=svc.defaults)
    errors = [f.as_dict() for f in findings if f.severity == "error"]
    external_item_sources = set(manifest.get("outputs") or []) if manifest.get("accepts_items") else set()
    graph = expand(doc, svc.lib_dir, external_item_sources=external_item_sources) if not errors else None
    store = Store(Path(os.environ.get("AX_DATA_DIR", "/data")) / "subloop.db")
    if graph is None:
        # 検査に外れたグラフは動かさない（S-7.7）。HTTP だけ立てて /status に理由を出す
        from .graph import Graph
        graph = Graph(entry="none", item_source=None, max_parallel_items=1, nodes={}, edges=[])
    ex = Executor(svc=svc, graph=graph, manifest=manifest, registry=registry, store=store)
    if pid == "scheduled-publisher":
        from plugins.timer.scheduled_publisher.integration import bind_runtime
        bind_runtime(ex)
    if pid in ("x-posts", "x-posts-draft"):
        ex.mode = "collect_only"
    from axcommon import creds

    app = build_app(ex, apply_token=creds.read_secret_optional("control-apply-token"), ext_dir=str(plugin_dir / "ext"), run_loops=not errors, graph_errors=errors or None)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("AX_HEALTH_PORT", "8080")), log_level="warning")  # noqa: S104  コンテナの中。公開は compose が決める（ports を持たない）
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Expose the X subloop node to control's graph registry."""
from pathlib import Path
import importlib.util

_path = Path(__file__).resolve().parents[2] / "timer" / "x-posts" / "nodes" / "x" / "posts.py"
_spec = importlib.util.spec_from_file_location("ax_x_posts_node", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TYPE = _module.TYPE
schema = _module.schema
META = _module.META
run = _module.run
reconcile = _module.reconcile

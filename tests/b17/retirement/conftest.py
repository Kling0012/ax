"""Keep retirement tests independent of installation-specific configuration."""

import importlib.util
from pathlib import Path
import sys


_site_name = "tools.cutover.retirement.site"
_example = Path(__file__).resolve().parents[3] / "tools/cutover/retirement/site.example.py"
_spec = importlib.util.spec_from_file_location(_site_name, _example)
assert _spec is not None and _spec.loader is not None
_site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_site)
sys.modules[_site_name] = _site

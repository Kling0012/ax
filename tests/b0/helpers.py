"""tests/b0 の補助。tools/*.py はパッケージではないので、ファイルから読み込む。"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location("axtool_" + name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

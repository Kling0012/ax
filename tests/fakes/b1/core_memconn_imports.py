"""core/memory-connector（ハイフン入りで import できない）の memconn を、試験から使えるようにする橋。"""
import sys
from pathlib import Path

_dir = str(Path(__file__).resolve().parents[3] / "core" / "memory-connector")
if _dir not in sys.path:
    sys.path.insert(0, _dir)

from memconn.mcp import Indeterminate, McpClient, NotSent, ToolError  # noqa: E402,F401
from memconn import checks, service  # noqa: E402,F401   （memconn.app は starlette を要るので、HTTP の試験が直接 import する）

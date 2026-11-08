from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "host/lib"))
sys.path.insert(0, str(ROOT / "tests/fakes/b12b1/ssh"))

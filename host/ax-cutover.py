#!/usr/bin/env python3
"""Run from the checked-out AX tree; no installation or service start."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.cutover.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

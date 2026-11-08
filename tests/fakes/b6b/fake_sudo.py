#!/usr/bin/env python3
"""偽の sudo（B6b の試験用）。-n と --preserve-env=… を読み捨てて、残りをそのまま実行する。呼び出しを FAKE_SUDO_LOG に追記する。"""
import json
import os
import sys

argv = sys.argv[1:]
seen = []
while argv and argv[0].startswith("-"):
    seen.append(argv.pop(0))
log = os.environ.get("FAKE_SUDO_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"options": seen, "cmd0": argv[0] if argv else None}) + "\n")
if not argv:
    sys.exit(1)
os.execvp(argv[0], argv)

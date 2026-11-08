#!/usr/bin/env python3
"""host/ax-fw.sh の試験用の偽の tailscale / ufw / iptables（実物を呼ばない）。1 本のスクリプトを名前（引数 1 = tailscale|ufw|iptables）で使い分ける。

環境変数:
  FAKE_TS_MAP     {"host": "ip", "": "自身の ip"} の JSON ファイル（tailscale ip -4 [host]）
  FAKE_FW_STATE   状態のディレクトリ。calls.log に呼び出しを追記、loaded.txt に reload した規則を置く
  FAKE_AFTER_RULES  ufw reload が読む after.rules のパス
  FAKE_UFW_FAIL=reload  reload を失敗させる
"""
import json
import os
import sys
from pathlib import Path

tool, args = sys.argv[1], sys.argv[2:]
state = Path(os.environ.get("FAKE_FW_STATE", "."))
state.mkdir(parents=True, exist_ok=True)
with (state / "calls.log").open("a") as f:
    f.write(json.dumps([tool] + args) + "\n")

if tool == "tailscale":
    m = json.loads(Path(os.environ["FAKE_TS_MAP"]).read_text())
    host = args[2] if len(args) >= 3 else ""
    if args[:2] != ["ip", "-4"] or host not in m:
        sys.exit(1)
    print(m[host])
elif tool == "ufw":
    if args[:1] == ["reload"]:
        if os.environ.get("FAKE_UFW_FAIL") == "reload":
            sys.exit(1)
        text = Path(os.environ["FAKE_AFTER_RULES"]).read_text()
        inblock, rules = False, []
        for line in text.splitlines():
            if line.strip() == "# BEGIN AX":
                inblock = True
            elif line.strip() == "# END AX":
                inblock = False
            elif inblock and line.startswith("-A DOCKER-USER"):
                rules.append(line)
        (state / "loaded.txt").write_text("\n".join(rules) + "\n")
    elif args[:2] == ["status", "numbered"]:
        print("Status: active\n[ 1] 22/tcp ALLOW IN Anywhere")
    elif args[:1] == ["allow"]:
        pass
    else:
        sys.exit(1)
elif tool == "iptables":
    if args[:2] == ["-S", "DOCKER-USER"]:
        print("-N DOCKER-USER")
        p = state / "loaded.txt"
        if p.exists():
            print(p.read_text(), end="")
    else:
        sys.exit(1)
else:
    sys.exit(2)

#!/usr/bin/env python3
"""偽の ax（T-150、tools/cutover_note.py の試験用）。PATH の先頭に `ax` の名前で置く。`ax claim renew cutover --json …` だけを答える。

環境変数:
  FAKE_AX_MODE   holder（既定）/ conflict（終了コード 8）/ conflict_json（error.code=conflict、終了コード 1）/ garbage（JSON でない）/
                 not_holder（holder: false）/ bad_owner（owner の形が違う）/ missing（終了コード 127 相当: 実行はするが何も出さない）
  FAKE_AX_OWNER  owner の JSON（既定 {"principal":"hub-main","session":"cutover-20261107-abc","via":"ssh"}）
  FAKE_AX_CAUSE  claim_event_id（既定は固定の ULID）
  FAKE_AX_LOG    あれば、引数を 1 行ずつ追記する
"""
import json
import os
import sys

log = os.environ.get("FAKE_AX_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_AX_MODE", "holder")
owner = json.loads(os.environ.get("FAKE_AX_OWNER", '{"principal":"hub-main","session":"cutover-20261107-abc","via":"ssh"}'))
cause = os.environ.get("FAKE_AX_CAUSE", "01HZX0000000000000000000AB")
if sys.argv[1:4] != ["claim", "renew", "cutover"]:
    print(json.dumps({"ok": False, "error": {"code": "usage"}}))
    sys.exit(2)
if mode == "conflict_empty":
    sys.exit(8)
if mode == "missing":
    sys.exit(127)
if mode == "garbage":
    print("not json")
    sys.exit(0)
if mode == "conflict":
    print(json.dumps({"ok": False, "error": {"code": "conflict"}}))
    sys.exit(8)
if mode == "conflict_json":
    print(json.dumps({"ok": False, "error": {"code": "conflict"}}))
    sys.exit(1)
if mode == "not_holder":
    print(json.dumps({"ok": True, "data": {"holder": False}}))
    sys.exit(0)
if mode == "bad_owner":
    owner = {"principal": "X", "session": "s", "via": "ssh"}
print(json.dumps({"ok": True, "data": {"holder": True, "owner": owner, "claim_event_id": cause}}))
sys.exit(0)

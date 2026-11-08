#!/usr/bin/env bash
# tailnet の端末表を ${ax_state_dir}/gate/peers.json に原子的に更新する。S-6.14、F-9.4。
# 使い方: ax-peers.sh [--profile FILE]
# profile の既定は ~/.config/ax/profile/default.yaml。AX_PROFILE_FILE で変更でき、--profile が優先。
# 実行するのは go の後。ここでは tailscaled の LocalAPI を gate へ渡さない。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PROFILE="${AX_PROFILE_FILE:-${HOME}/.config/ax/profile/default.yaml}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) [ "$#" -ge 2 ] || { echo "ax-peers: --profile に値が要る" >&2; exit 2; }; PROFILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "ax-peers: 知らない引数: $1" >&2; exit 2 ;;
  esac
done
profile() { PYTHONPATH="$REPO" python3 -P -m axcommon.profile --profile "$PROFILE" --print "$1"; }
STATE="$(profile AX_STATE_DIR)" || exit 1
OUT="$STATE/gate/peers.json"
mkdir -p "$STATE/gate"
umask 077
"${AX_PEERS_TAILSCALE:-sudo -n tailscale}" status --json | PYTHONPATH="$REPO" python3 -P - "$OUT" 3<&0 <<'PY'
import json
import os
import sys
from pathlib import Path

out = Path(sys.argv[1])
document = json.load(os.fdopen(3, "r", encoding="utf-8"))
profiles = document.get("User", {})
records = [document.get("Self", {})]
peers = document.get("Peer", {})
records.extend(peers.values() if isinstance(peers, dict) else [])
mapping = {}
for item in records:
    if not isinstance(item, dict):
        continue
    profile = profiles.get(str(item.get("UserID")), {})
    login = profile.get("LoginName") if isinstance(profile, dict) else None
    device = item.get("DNSName") or item.get("HostName")
    device_id = item.get("StableID")
    if not all(isinstance(value, str) and value for value in (login, device, device_id)):
        continue
    row = {"device": device.rstrip("."), "device_id": device_id, "os": item.get("OS", ""),
           "user_login": login, "online": bool(item.get("Online", False))}
    for address in item.get("TailscaleIPs", []):
        if isinstance(address, str):
            mapping[address] = row
raw = json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
temp = out.with_name("." + out.name + ".new")
with open(temp, "wb") as f:
    os.fchmod(f.fileno(), 0o600)
    f.write(raw)
    f.flush()
    os.fsync(f.fileno())
os.replace(temp, out)
PY

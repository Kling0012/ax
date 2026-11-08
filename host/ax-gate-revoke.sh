#!/usr/bin/env bash
# HQ が使えないとき、1 席だけを fail-closed で失効する。S-6.14、F-9.4。
# 使い方: ax-gate-revoke.sh [--profile FILE] <seat_id>
# profile の既定は ~/.config/ax/profile/default.yaml。AX_PROFILE_FILE で変更でき、--profile が優先。
# 印は消さない。HQ の seat.revoke が記録と台帳を更新できるまで、gate は次の要求から拒否する。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PROFILE="${AX_PROFILE_FILE:-${HOME}/.config/ax/profile/default.yaml}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) [ "$#" -ge 2 ] || { echo "ax-gate-revoke: --profile に値が要る" >&2; exit 2; }; PROFILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) break ;;
  esac
done
[ "$#" -eq 1 ] || { echo "ax-gate-revoke: seat_id を1つ指定する" >&2; exit 2; }
SEAT_ID="$1"
case "$SEAT_ID" in *[!a-z0-9-]*|""|??) echo "ax-gate-revoke: 不正な seat_id" >&2; exit 2 ;; esac
[ "${#SEAT_ID}" -le 32 ] || { echo "ax-gate-revoke: 不正な seat_id" >&2; exit 2; }
PROFILE_STATE="$(PYTHONPATH="$REPO" python3 -P -m axcommon.profile --profile "$PROFILE" --print AX_STATE_DIR)" || exit 1
umask 077
DIR="$PROFILE_STATE/gate/revoked.d"
mkdir -p "$DIR"
: > "$DIR/$SEAT_ID"
chmod 0600 "$DIR/$SEAT_ID"

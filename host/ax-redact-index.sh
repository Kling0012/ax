#!/usr/bin/env bash
# 秘密置き場の索引 ${ax_state_dir}/redact/index を作る（F-18.6）。B0 の最小版を B8 が仕上げた。
# 仕上げ: 起動時（ax-up.sh が索引が無ければ作る）と 1 時間ごと（host/ax-hostwatch.sh の周期から）の再作成、作り直しの失敗を health.alert に書く
#         （索引が古いままの間は redact が他の基準だけで動く。S-15.3）、archive/ の秘密が索引に入ったことの確認（host/lib/hostwatch/indexverify.py）。
#
# 使い方: host/ax-redact-index.sh [--secrets-dir DIR] [--state-dir DIR] [--profile FILE]
#   --secrets-dir  既定は profile の secrets_dir
#   --state-dir    既定は profile の ax_state_dir。索引は <state-dir>/redact/index（先頭行がソルト、権限0644）
# profile の既定は ~/.config/ax/profile/default.yaml。AX_PROFILE_FILE で変更でき、--profile が優先。
# ソルトは既存の索引があれば引き継ぐ。ファイルの中身を標準出力に出さない。
# 終了コード: 0 成功 / 1 失敗（索引は書き換えない）/ 2 使い方の誤り
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PROFILE="${AX_PROFILE_FILE:-${HOME}/.config/ax/profile/default.yaml}"
SECRETS=""
STATE=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --secrets-dir) [ "$#" -ge 2 ] || { echo "ax-redact-index: --secrets-dir に値が要る" >&2; exit 2; }; SECRETS="$2"; shift 2 ;;
    --state-dir) [ "$#" -ge 2 ] || { echo "ax-redact-index: --state-dir に値が要る" >&2; exit 2; }; STATE="$2"; shift 2 ;;
    --profile) [ "$#" -ge 2 ] || { echo "ax-redact-index: --profile に値が要る" >&2; exit 2; }; PROFILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "ax-redact-index: 知らない引数: $1" >&2; exit 2 ;;
  esac
done

pyprofile() { PYTHONPATH="$REPO" python3 -P -m axcommon.profile --profile "$PROFILE" --print "$1"; }
[ -n "$SECRETS" ] || SECRETS="$(pyprofile AX_SECRETS_DIR)" || { echo "ax-redact-index: profile から secrets_dir を読めない" >&2; exit 1; }
[ -n "$STATE" ] || STATE="$(pyprofile AX_STATE_DIR)" || { echo "ax-redact-index: profile から ax_state_dir を読めない" >&2; exit 1; }

alert() {  # 失敗を hostwatch.log へ health.alert として足す（状態置き場がある場合だけ。オブザーバーが戻ったら時系列記録へ写す）
  [ -d "$STATE" ] || return 0
  local host
  host="$(pyprofile AX_HOST 2>/dev/null || echo unknown)"
  PYTHONPATH="$REPO:$HERE/lib${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m hostwatch.hostlog "$STATE" "$host" health.alert "redact_index:index" \
    check=redact_index target=index severity=warn destination=manager "detail=$1" >/dev/null 2>&1 || true
}

read -r -a INDEX_SUDO <<< "${AX_REDACT_SUDO:-}"
if ! "${INDEX_SUDO[@]}" env PYTHONPATH="$REPO" python3 -P -m axcommon.redact_index --secrets-dir "$SECRETS" --out "$STATE/redact/index"; then
  alert "索引を作り直せない。古い索引のまま（redact は他の基準だけで動く）"
  exit 1
fi
if ! "${INDEX_SUDO[@]}" env PYTHONPATH="$REPO:$HERE/lib${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m hostwatch.indexverify --secrets-dir "$SECRETS" --index "$STATE/redact/index"; then
  alert "archive/ の秘密が索引に入っていない"
  exit 1
fi

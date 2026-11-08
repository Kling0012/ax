#!/usr/bin/env bash
# profile から環境変数ファイル（${ax_state_dir}/deploy/profile.env）を作る（F-18.6）。B0 の最小版を B8 が仕上げた。
# 仕上げ: bind_addr が tailnet のアドレス（tailscale ip -4）か 127.0.0.1 であることの検査、hq_access の構造、S-16.3 の項目の型、
#         ax-hq の uid・gid の取り込み（居なければ空）、書き込み前後の差分の表示（変数の名前だけ。値は伏せる）。本体は host/lib/hostwatch/envfinish.py。
#
# 使い方: host/ax-env.sh [--profile FILE] [--check] [--out FILE]
#   --profile  既定 ~/.config/ax/profile/default.yaml（AX_PROFILE_FILE で変更でき、--profile が優先）
#   --check    検査だけ（何も書かない）
#   --out      出力先を変える（試験用）。既定 ${ax_state_dir}/deploy/profile.env
# 終了コード: 0 成功 / 1 必須の項目が無い、パスが相対、bind_addr が空・不正、型が違うなど（何も書かない）/ 2 使い方の誤り
# 秘密は入れない。値を標準出力に出さない。YAML と変換は python3（標準ライブラリと PyYAML）に任せる。
# 環境変数: AX_PROFILE_FILE（--profile が無い場合の profile）、AX_ENV_TAILSCALE（既定 "tailscale ip -4"）、AX_ENV_ID（既定 "id"）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PROFILE="${AX_PROFILE_FILE:-${HOME}/.config/ax/profile/default.yaml}"
CHECK=0
OUT=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) [ "$#" -ge 2 ] || { echo "ax-env: --profile に値が要る" >&2; exit 2; }; PROFILE="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    --out) [ "$#" -ge 2 ] || { echo "ax-env: --out に値が要る" >&2; exit 2; }; OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "ax-env: 知らない引数: $1" >&2; exit 2 ;;
  esac
done

args=(--profile "$PROFILE")
[ "$CHECK" -eq 1 ] && args+=(--check)
[ -n "$OUT" ] && args+=(--out "$OUT")
exec env PYTHONPATH="$REPO:$HERE/lib${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m hostwatch.envfinish "${args[@]}"

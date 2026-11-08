#!/usr/bin/env bash
# 環境モジュールの導入スクリプトの雛形（docs/ENV-MODULE.md §11 の形）。
# 実物（host/env-module-min/install.sh）が実在します。その雛形の動作は install.sh --check で確かめられます。
# この雛形は、新しいホストへ導入するときの手順の example です。
# 実物はこの雛形を、環境ごとの前提（配置場所、版）に合わせて複製して使います。
#
# 使い方（雛形）: install-env-module.sh [--dest DIR] [--check]
#   --dest   既定 ${AX_ENV_HOME:-$HOME/.ax-env}（ssh-host）、/ax-env（container）
#   --check  何も書かず、置き方が正しいかだけ確かめる（終了コード 0 が正しい）
# 終了コード（実物と同じ）: 0 成功 / 1 current が別の版を指していて切り替えない / 2 使い方の誤り
#
# やること（順番）:
#   1. check: 足りない前提（シェル、tar、必要な隔離の機能）を表示して止まる。何も変えない。
#   2. 設置: releases/<module_version>/ に配置し、current を向ける。既に同じ版があれば何もしない。
#   3. 印を作る: bin -> current/bin、entrypoints -> current/entrypoints を 1 回だけ作る（authorized_keys の行は変えない）。
# 入力ログは $AX_ENV_HOME/install.log に追記します。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 実物（host/env-module-min/install.sh）の変数に合わせます。ここは雛形なので処理は書いていません。
SRC="$HERE/releases/<module_version>" # 雛形のパス。実物は releases/b6 の複製です。
DEST="${AX_ENV_HOME:-$HOME/.ax-env}"
CHECK=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dest) [ "$#" -ge 2 ] || { echo "install: --dest に値が要る" >&2; exit 2; }; DEST="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "install: 知らない引数: $1" >&2; exit 2 ;;
  esac
done
case "$DEST" in /*) ;; *) echo "install: --dest は絶対パス" >&2; exit 2 ;; esac
[ -x "$SRC/bin/ax-env-run" ] || { echo "install: $SRC/bin/ax-env-run が無い" >&2; exit 2; }
echo "install-env-module: 雛形です。実物は host/env-module-min/install.sh を使ってください" >&2
exit 0

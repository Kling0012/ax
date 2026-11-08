#!/usr/bin/env bash
# ax-env-run の B6 最小版を ~/.ax-env に置く（F-18.2、ENV-MODULE §3 の配置）。冪等。go の後に operator が実行する（B6 の最初）。
# 作る物: $DEST/releases/b6/（このディレクトリの releases/b6 の複製）、current -> releases/b6、bin -> current/bin。
# authorized_keys には触れない（行の形は docs/b6b/launcher-ssh-key.md）。削除しない（違う内容の releases/b6 は archive/ へ移す）。
# 使い方: install.sh [--dest DIR] [--check]
#   --dest   既定 ${AX_ENV_HOME:-$HOME/.ax-env}
#   --check  何も書かず、置き方が正しいかだけ確かめる（終了コード 0 が正しい）
# 終了コード: 0 成功 / 1 current が別の版を指していて切り替えない（B14b が先に入っている） / 2 使い方の誤り
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/releases/b6"
DEST="${AX_ENV_HOME:-$HOME/.ax-env}"
CHECK=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dest) [ "$#" -ge 2 ] || { echo "install: --dest に値が要る" >&2; exit 2; }; DEST="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "install: 知らない引数: $1" >&2; exit 2 ;;
  esac
done
case "$DEST" in /*) ;; *) echo "install: --dest は絶対パス" >&2; exit 2 ;; esac
[ -x "$SRC/bin/ax-env-run" ] || { echo "install: $SRC/bin/ax-env-run が無い" >&2; exit 2; }

same_release() { diff -rq -x __pycache__ "$SRC" "$DEST/releases/b6" >/dev/null 2>&1; }

if [ "$CHECK" -eq 1 ]; then
  [ -d "$DEST/releases/b6" ] && same_release || { echo "install: releases/b6 が無いか、内容が違う" >&2; exit 1; }
  [ "$(readlink "$DEST/current" 2>/dev/null)" = "releases/b6" ] || { echo "install: current が releases/b6 を指していない" >&2; exit 1; }
  [ "$(readlink "$DEST/bin" 2>/dev/null)" = "current/bin" ] || { echo "install: bin が current/bin を指していない" >&2; exit 1; }
  echo "install: OK"
  exit 0
fi

cur="$(readlink "$DEST/current" 2>/dev/null || true)"
if [ -n "$cur" ] && [ "$cur" != "releases/b6" ]; then
  echo "install: current は $cur を指している。切り替えない（B14b の版が入っている）" >&2
  exit 1
fi

umask 077
mkdir -p "$DEST/releases" "$DEST/archive"
chmod 700 "$DEST"
if [ -d "$DEST/releases/b6" ] && ! same_release; then
  mv "$DEST/releases/b6" "$DEST/archive/b6-$(date -u +%Y%m%dT%H%M%SZ)"
fi
if [ ! -d "$DEST/releases/b6" ]; then
  mkdir -p "$DEST/releases/b6"
  (cd "$SRC" && tar --exclude=__pycache__ -cf - .) | (cd "$DEST/releases/b6" && tar -xf -)
fi
ln -sfn releases/b6 "$DEST/current"
ln -sfn current/bin "$DEST/bin"
echo "install: $DEST に b6 を置いた。強制コマンドは $DEST/bin/ax-env-run --caller launcher"

#!/usr/bin/env bash
# AX の CI（F-14.1）。引数なし。終了コード0が「CI が通る」。
# 手順: (1) schemas/ のメタスキーマ検査  (2) ax-graph-check  (3) compose の検査  (4) pytest
# 実行場所: ビルド用ホストで `sudo -n docker run --rm -v "$PWD":/src -w /src ax-ci:<版> tools/ci.sh`。
# 環境変数（試験と部分実行用。既定のままなら4つとも実行する）:
#   AX_CI_STEPS        実行する手順の番号（既定 "1 2 3 4"）
#   AX_CI_PYTHON       python の実行ファイル（既定 python3）
#   AX_CI_PYTEST_CMD   (4) のコマンド（既定 "<python> -m pytest -p no:cacheprovider --import-mode=importlib tests"）
#   AX_CI_COMPOSE_DIR  (3) で compose を探す根（既定 リポジトリの根）。試験で壊れた compose を入れるために使う
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
: "${TMPDIR:?TMPDIR が未設定。一時置き場（HDD）を指す環境変数を設定する（F-1）}"
PY="${AX_CI_PYTHON:-python3}"
STEPS="${AX_CI_STEPS:-1 2 3 4}"
CDIR="${AX_CI_COMPOSE_DIR:-$ROOT}"

want() { case " $STEPS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
say() { printf '== %s\n' "$*"; }
fail() { printf 'CI 失敗: %s\n' "$*" >&2; exit 1; }

if want 1; then
  say "(1) schemas/ のメタスキーマ検査（Draft 2020-12）"
  PYTHONPATH="$ROOT" "$PY" -P -m axcommon.schemas --check || fail "(1) schemas の検査"
fi

if want 2; then
  say "(2) ax-graph-check（plugins/subloop/ と templates/）"
  if [ -f tools/ax_graph_check.py ]; then
    targets=()
    [ -d plugins/subloop ] && targets+=(plugins/subloop)
    [ -d templates ] && targets+=(templates)
    if [ "${#targets[@]}" -gt 0 ]; then
      PYTHONPATH="$ROOT" "$PY" tools/ax_graph_check.py "${targets[@]}" || fail "(2) ax-graph-check"
    else
      say "    対象のディレクトリが無い。飛ばす"
    fi
  else
    say "    tools/ax_graph_check.py がまだ無い（B7 で作る）。飛ばす"
  fi
fi

if want 3; then
  say "(3) compose の検査（デーモンに接続しない。ダミーの値は tools/ci-fixtures/）"
  command -v docker >/dev/null 2>&1 || fail "(3) docker の CLI が無い（ax-ci イメージには入っている。F-14.1）"
  shopt -s nullglob
  files=(-f "$CDIR/compose.yaml")
  for f in "$CDIR"/compose.d/*.yaml; do files+=(-f "$f"); done
  shopt -u nullglob
  errf="$(mktemp "$TMPDIR/ci-compose-err.XXXXXX")"
  outf="$(mktemp "$TMPDIR/ci-compose-out.XXXXXX")"
  trap 'rm -f "$errf" "$outf"' EXIT
  rc=0
  # --profile '*' は、プロファイルで止めてあるサービスも検査に入れるため（B0 の axcommon-selfcheck など）
  DOCKER_HOST="unix:///nonexistent" docker compose -p ax-ci --project-directory "$CDIR" \
    --env-file "$ROOT/tools/ci-fixtures/ci.env" "${files[@]}" --profile '*' config --format json >"$outf" 2>"$errf" || rc=$?
  if [ "$rc" -ne 0 ]; then cat "$errf" >&2; fail "(3a) docker compose config の終了コード $rc"; fi
  if grep -q 'variable is not set' "$errf"; then cat "$errf" >&2; fail "(3b) 未設定の変数がある。ci.env に足す（F-18.6）"; fi
  "$PY" "$ROOT/tools/compose_check.py" "$outf" || fail "(3c-e) compose_check"
fi

if want 4; then
  say "(4) pytest"
  if [ -n "${AX_CI_PYTEST_CMD:-}" ]; then
    # shellcheck disable=SC2086
    $AX_CI_PYTEST_CMD || fail "(4) pytest"
  else
    "$PY" -m pytest -p no:cacheprovider --import-mode=importlib tests || fail "(4) pytest"
  fi
fi

say "CI 合格（手順: $STEPS）"

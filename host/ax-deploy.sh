#!/usr/bin/env bash
# 中核の差し替えとサブループの追加の配備（S-16.7、F-18.3 の 4）。ホスト見張りが要求 deploy を読んだときに呼ぶ。手で呼んでもよい。
# 手順: 受け入れの印（タグ accepted/<sha>）のある main だけを AX_HOME に取り込み、AX_TAG を current.env に書き、docker compose build、
#       変わった・新しいサービスを host/ax-up.sh で 1 つずつ起動（healthz の後に登録）。中核が通らなければ前のタグで起動し直す。
# 終了コード: 0 完了 / 1 失敗 / 4 別の配備が実行中 / 10 印がなく rejected（何も変えない）/ 11 中核が通らず前のタグへ戻した / 12 サブループの一部が起動しなかった
# 環境変数: AX_PROFILE、AX_HOME、AX_HOSTWATCH_SUDO（既定 "sudo -n"）。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$REPO:$HERE/lib${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -P -m hostwatch deploy

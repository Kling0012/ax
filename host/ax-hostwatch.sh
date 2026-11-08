#!/usr/bin/env bash
# ホスト見張りの 1 周期（S-11.5、D-10.3）。host/ax-hostwatch.timer が host_watch_s ごとに host/ax-hostwatch.service 経由で呼ぶ。
# コンテナの外で operator として動く。特権操作は sudo -n（S-11.6）。本体は host/lib/hostwatch/（Python。試験では偽の実行器に差し替える）。
# 登録制（F-14.2）: ${ax_state_dir}/hostwatch/registered/<サービス名> にある compose のサービスだけを見張る。commander_unit には触れない。
# 環境変数: AX_PROFILE、AX_HOME、AX_HOSTWATCH_SUDO（既定 "sudo -n"）。終了コード: 0（周期を回した・重なりで飛ばした）/ 1（profile を読めない）
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$REPO:$HERE/lib${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -P -m hostwatch cycle

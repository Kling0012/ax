# 実環境でしか通せない確認（この束では未実行）

| # | 確認 | 場所 | 理由 |
|---|---|---|---|
| R1 | T-104: 本物の `sudo -n docker compose … up -d` / `restart` で、止めた部品が起動し直る | hub-host | sudo・docker の実行が禁止 |
| R2 | T-104: `sudo -n` を失敗させた環境（sudoers を一時的に外す等）で、特権操作だけが止まり、カードが 1 件出て、観測・通知・入口・台帳が動き、戻すと `host.sudo_restored` で再開する | hub-host | 同上。偽の sudo での分岐は確認済み |
| R3 | T-104: `intake` が書いた `deploy` で、印のある main だけが取り込まれ、実際のビルド・起動・healthz の後の登録が行われる。印のない main は `rejected` | hub-host | docker build と git の本物の bare リポジトリが要る |
| R4 | T-104: 中核の healthz が通らないときの前のタグでの起動し直しと `deploy.rolled_back`、ToDo | hub-host | 同上 |
| R5 | T-104: 能力 `docker`（`hub-host-local`）が `sudo -n docker info` の成否で `ok` / `unverified` になる（担当は資源台帳の `probe`。b8 の範囲外） | hub-host | 別の束 |
| R6 | T-12: 部品を止めると検出する。オブザーバーを止めるとホスト見張りが起動し直す。監視を先に起動する | hub-host | compose を実際に立てる |
| R7 | 本物の systemd での `ax-hostwatch.timer` の周期、`loginctl enable-linger` | hub-host | systemctl が禁止 |
| R8 | `ax-env.sh` の `bind_addr` の検査を、本物の `tailscale ip -4` で通す | hub-host | tailscale の実行は偽の物で代替 |
| R9 | T-150(g): 変更サブループが `cutover-time` の記録を待つ処理 | 変更受付の束の試験 | 別の束 |
| R10 | T-208: 本物の `ax claim` との接続で `cutover_note.py` を通す | b12b3 | 本物の `ax` が要る |
| R11 | 本物の pytest・shellcheck・yamllint | `ax-ci` の中 | この環境に無い（導入が禁止） |
| R12 | B0 の T-120 を同じ結果で再実行する | 統合時 | この副パッケージでは担当の tests/b8/ だけを実行する制限がある |

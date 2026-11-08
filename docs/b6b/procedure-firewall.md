# ufw の規則と `DOCKER-USER` の規則

対象ホストの運用者が、現在のネットワーク設定と影響を確認してから実行する。実環境での適用は未検証。
スクリプトは `host/ax-fw.sh`。冪等で、書き換える前に控えを作る。

## 手順

1. 控えの置き場を確かめる。既定は `~/.local/share/ax/archive/ax-cutover-<UTC日付>/fw/`。`--archive-dir` で変えられる。
2. 規則の中身を見る（何も書かない）。
   - `host/ax-fw.sh rules`: `DOCKER-USER` のブロック。環境の宛先は profile の `env_ssh_targets`、退避の宛先は `archive_dest` を `tailscale ip -4` で解決する。
   - `host/ax-fw.sh ufw-rules`: ufw の許可規則の一覧。
3. ufw の許可規則を入れる: `host/ax-fw.sh ufw-apply`。入れる前に `ufw status numbered` を控えの置き場へ保存する。
4. `DOCKER-USER` の規則を入れる: `host/ax-fw.sh apply`。
   - 書き換える前に `/etc/ufw/after.rules` を `cp -p` で控え、`iptables -S DOCKER-USER` の出力を保存する。
   - `# BEGIN AX` から `# END AX` の間だけを書き換える。印が無ければ末尾に足す。印が壊れていれば何も書かない。
   - 内容が同じなら何もしない（控えも reload も無い）。違えば書き換えて `ufw reload` し、`iptables -S DOCKER-USER` に規則が見えることを確かめる。
5. 環境を台帳に足したときも、profile の `env_ssh_targets` を直して `host/ax-fw.sh apply` を流す。`check` で一致を確かめられる。

## 規則の要点

- `ax-work` の `AX_FW_RESOURCES_IP`（`resources`）だけが、登録済みの環境のポートへ出られる。
- 退避コネクタ `archiver`（`ax-core` の `AX_FW_ARCHIVER_IP`）だけが、`archive_dest` のポートへ出られる。
- ほかの `ax-core`、`ax-work`、`ax-job` から `tailscale0` への転送は捨てる。
- hub-host 自身の tailnet アドレスへの通信（8377、8908）は INPUT なので、この規則の影響を受けない。

## Docker ネットワークのアドレス割当

動的割当範囲を設定した新しいネットワークでは、動的に接続する管理外のコンテナもその範囲からアドレスを受け取る。管理外の接続は必要なものに限る。手動で指定した固定 IP や、他のコンテナのネットワーク名前空間を共有する接続は、動的割当範囲だけでは制限できない。

既に作成された Docker ネットワークには Compose の IPAM 設定の変更は反映されない。既存ネットワークに適用するときは、影響と停止時間を確認し、移行を計画する。

## 失敗したとき

- tailnet 名を解決できない環境が 1 つでもあれば、何も書かずに終了コード 1。
- `ufw reload` が失敗したら終了コード 3。`after.rules` は更新済みなので、控えの `after.rules.<UTC日時>` から戻す。
- 規則を入れられない場合や他の Docker 通信に影響する場合は、代替手段の決定が必要。

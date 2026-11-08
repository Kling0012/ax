# 実環境でしか通せない確認

B6b は docker、sudo、ssh、ufw、ネットワークを使わずに作った。次は go の後に hub-host で行う。結果は `status/b6b.md` の表に写す。

## T-40 規則と隔離

1. 偽の runner（`sleep` するだけ。試験用に `config/runners/` へ一時的に置く）で `job-start` し、`sudo -n docker inspect` で次を確かめる。
   `Privileged` が false、`CapAdd` が空、`ReadonlyRootfs` が true、`HostConfig.NetworkMode` が `ax-job`、`Mounts` が `/work` と `/tmp-hdd` の 2 つだけ、ソケットの bind が無い。
2. `ax-job` のコンテナから `198.51.100.1:3425` と外部の任意のホストへ直接届かない。
3. `egress-proxy` 経由で、許可先にあるホストの 443 は通り、ないホストへの `CONNECT` は 403。
4. `ax-env-run` に未知のサブコマンドとイメージ指定を送ると拒否される。
5. launcher が使えない間（鍵、SSH、`sudo -n` の失敗）、`local` の新しいジョブだけが `parked` になる（worker-pool 側。b6a と合わせて確かめる）。

## T-118 追加の読み取り専用マウント

1. `ro_mounts: [sod-gamedata]` のジョブで、コンテナの中から `/mnt/sod-gamedata` に書けない。
2. `/tmp-hdd/moddeploy` に非 root で書ける。
3. `~/dev/sod-gamedata` が無い状態で終了コード 3（`mount_missing`）になり、その repo のジョブだけが `parked` になる（worker-pool 側）。

## T-122 作業の場所の共有と排他

1. 同じ `workdir_id` の 2 つ目のジョブを、launcher を直接呼んで終了コード 4 にする。
2. 続きからの再開で、元の連鎖の HOME に会話の記録があり、`claude --resume` が動く。記録の置き場の名前は `-work`。

## T-124 環境への経路の制限

1. `198.51.100.11` のコンテナから、台帳の環境のポート（`legacy-host` の 22 を含む）に届く。
2. `ax-work` の他のコンテナ、`ax-core` のコンテナ（`archiver` を除く）、`ax-job` のコンテナからは、tailnet の他の機に届かない。
3. `archiver`（`192.0.2.12`）から archive-host の `archive_dest` のポートにだけ届き、他のポートと他の機には届かない。
4. `ax-core` から hub-host 自身の 8377 と 8908 に届く。docker の他のネットワークの外向きは変わらない。
5. `host/ax-fw.sh apply` を 2 回流して同じ結果になり、1 回目の前に控えができている。

## T-182 ゲートへの中継（b18a）

`egress-proxy` の素の HTTP の中継の口は B6b で作った。実際の MCP クライアントの通り方（絶対 URI を送るか、直接 IP を使うか）の実測は B18 で行う。
許可ファイルの `@gate <port>` の形は推奨の既定値で、B18 で確定する。

## 作業用イメージ

- `host/ax-deploy.sh`（b8）が `runtime/images/agent-cli/vendor/claude` を `cp -p` した後、`docker compose build` で `img-*` を組み立てる。
- `agent-cli` の確かめ（F-19.1）: runner `agent-cli`、最安の無料モデルで「OK とだけ返す」を `claude … -p --output-format json` に渡し、標準出力が `result` と `is_error` を持つ 1 つの JSON であること。
- `base-images.lock` のダイジェストを実測して固定する。

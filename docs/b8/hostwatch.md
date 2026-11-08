# ホスト見張り

ホストの systemd の timer が `host_watch_s` ごとに `host/ax-hostwatch.sh` を動かす。operator として動く。特権操作は `sudo -n`（S-11.6）。コンテナには Docker のソケットも sudo も渡さない。

## 1 周期（D-10.3）

1. `sudo -n true` を実行する。失敗なら 2・3・5 を行わず、`host.sudo_unavailable` を書く（成功から失敗に変わったときだけ）。戻ったら `host.sudo_restored`。溜まった要求は戻った後に処理する。
2. `sudo -n docker compose … ps --all --format json` で状態を見る。対象は `hostwatch/registered/<名前>` にある compose のサービスだけ。`exited`・`dead`・`created`・`unhealthy`・コンテナ無しを起動し直す。止まっている物は `up --no-deps -d`、動いている不調な物は `restart` を使う。未登録の依存サービスは起動しない。`ps` が失敗したときや JSON を読めないときは起動し直さず、再起動要求も次の周期へ残す。
3. `observer.beat` が `observer_stale_s` より古い（または無い）とき、`observer` を起動し直す。直後は `observer_stale_s` の間、続けて起動し直さない。
4. `commander_unit` に当たる名前と、`paseo`・`paseo-*` は対象から除く。登録のない部品は起動しない。
5. `host-requests/` の要求を実行する（[host-requests.md](host-requests.md)）。
6. 行ったこと（操作名、対象、結果）を `${ax_state_dir}/hostwatch.log` と journal に書く。ログの 1 行は S-3.2 の形の事象。オブザーバーが時系列記録へ写す。
7. 索引の再作成（`host/ax-redact-index.sh`）を起動時と 1 時間ごとに行う。sudo を要さない。

同じ周期の重なりは `flock -n` で飛ばす。

## 入れ方（go の後に実施者が行う。この束は入れない）

```text
loginctl enable-linger operator
mkdir -p ~/.config/systemd/user
cp host/ax-hostwatch.service host/ax-hostwatch.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ax-hostwatch.timer
```

- `host_watch_s` を変えたら timer の `OnUnitActiveSec` も同じ値にする。
- 監視の部品は最初に登録する。`host/ax-up.sh observer …` は `observer` を先頭に動かし、先に登録する。
- ホスト見張り自身は compose のサービスではない。印（`registered/hostwatch`）は作らない。timer が動いていることは `systemctl --user is-active ax-hostwatch.timer` で見る。

## 配備（`host/ax-deploy.sh`）

要求 `deploy` を受けて動く。順序と終了コードは `host/lib/hostwatch/deploy.py` の冒頭に書いた。要点は次のとおり。

- 取り込もうとする版にタグ `accepted/<sha>`（sha は 7〜40 桁の先頭一致）が無ければ `deploy.rejected` を書いて終わる。`AX_HOME` は変えない（終了コード 10）。
- 通れば `git merge --ff-only`、`current.env` を `previous.env` へ `cp -p`、新しい `AX_TAG` を書く。`vendor/claude` を置き、`docker compose --profile build build`。
- 変わった・新しいサービス（登録済みの物と新しい物だけ。一度きりのサービスと `img-*` のビルド専用サービスは除く）を、`host/ax-up.sh` で 1 つずつ起動する。サービスごとに healthz を待ち、登録を終えてから次へ進む。
- 中核が通らなければ `previous.env` に戻して同じサービスを起動し直し、`deploy.rolled_back` を書く（終了コード 11）。オブザーバーが ToDo（parked）にする。サブループの失敗はそのサービスだけ起動せず、他は続ける（終了コード 12）。ビルドか起動が失敗した配備は `pending.json` に変更前のタグとサービス定義を残す。同じ受け入れ済み HEAD の次回要求では、この記録から配備を再開する。

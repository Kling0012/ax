# `launcher-ssh-key` の生成と launcher の配置（F-18.2）

実行する機と人: hub-host の operator。go の後。B6 の最初。B6b は実行していない。

## 手順

1. 配置: `bash host/env-module-min/install.sh`。`~/.ax-env/releases/b6/` に複製し、`current -> releases/b6`、`bin -> current/bin` を作る。
   既に `current` が別の版を指していれば切り替えずに止まる。`--check` で置き方だけ確かめられる。
2. 鍵の生成: `ssh-keygen -t ed25519 -N "" -f "$SECRETS_DIR/launcher-ssh-key" -C ax-launcher`。`SECRETS_DIR` は profile の `secrets_dir`。権限は 600。
   既にあるときは作らない（上書きしない）。この鍵は worker-pool にだけ渡す。`resources` には渡さない。
3. `known_hosts`: `ssh-keyscan 198.51.100.1 > "$SECRETS_DIR/launcher-known-hosts"`。
4. `authorized_keys` に 1 行足す。先に `cp -p ~/.ssh/authorized_keys` で控えを作る（F-15.2 の 3 番）。行の形:

```text
restrict,from="198.51.100.10",command="<operator のホーム>/.ax-env/bin/ax-env-run --caller launcher" ssh-ed25519 <公開鍵> ax-launcher
```

   `command=` は絶対パスで書く。`--caller` は強制コマンドが固定するので、接続した側は変えられない。
5. ufw の規則（`procedure-firewall.md`）で、`ax-work` の `198.51.100.10` から `198.51.100.1:22` だけを許す。

## 確かめ（実環境）

- worker-pool のコンテナから `ssh -i … <EMAIL> job-list` が JSON 1 行を返す。
- 同じ接続で `ls` や `docker run --privileged x` を送ると終了コード 2 で拒否される。
- 別のアドレスの `ax-work` のコンテナからは 22 に届かない（ufw が拒否。`from=` も同じアドレスだけを許す）。

## resources 用の鍵

`resources` は別の鍵 `env-ssh-hub-host-local` と別の強制コマンド（`--caller env`）で同じファイルを呼ぶ（F-21）。生成は B14b の手順で行う。
B6 の最小版は `--caller env` の `job-*` を拒否し、ENV-MODULE §5 のサブコマンドは終了コード 70（`not_implemented`）で返す。

# ホストの3本の仕上げ（F-18.6 の B8 の列）

B0 の最小版（`tests/b0/test_t120_host_scripts.py`）の結果は変えない。

| スクリプト | 足した物 |
|---|---|
| `host/ax-env.sh` | `bind_addr` が `tailscale ip -4` の出力のどれか、または `127.0.0.1` であることの検査（`0.0.0.0` とホスト名は不可）。`hq_access` の構造（`iss`、`jwks_url`、`aud_secret`、`allowed_users_file`、`email_slot` が文字列）。S-16.3 の項目の型。`ax-hq` が居れば `AX_HQ_UID`・`AX_HQ_GID`。書き込み前後の差分（変数の名前だけ）。本体は `host/lib/hostwatch/envfinish.py` |
| `host/ax-up.sh` | 同じサービスの同時実行を `locks/up-<名前>.lock` の `flock -n` で拒否。`--dry-run`（何も作らない）。実行時は不正な profile と Paseo・司令塔の名前を状態置き場の作成前に拒否する。`$AX_HOME/host/ax` があれば `~/.local/bin/ax` へのリンク（B12 から。実ファイルは上書きしない）。結果の事象 `deploy.up`（`hostwatch.log` へ）。`observer` を先頭に起動し、healthz と登録を済ませてから次のサービスを起動する。待ちの間隔が 0 以下でも進む |
| `host/ax-redact-index.sh` | 失敗を `health.alert`（check `redact_index`）として `hostwatch.log` に書く。成功後に `secrets_dir/archive/` の値が索引にあることを確認する（`host/lib/hostwatch/indexverify.py`）。起動時の作成は `ax-up.sh`、1時間ごとはホスト見張りの周期 |

環境変数（試験用）: `AX_ENV_TAILSCALE`（既定 `tailscale ip -4`）、`AX_ENV_ID`（既定 `id`）、`AX_UP_SUDO`、`AX_UP_WAIT_S`、`AX_UP_INTERVAL_S`、`AX_BIN_DIR`。

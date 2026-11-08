# caller `ax` の鍵の発行（F-3.1）

実行する機と人: hub-host の operator。go の後。B6 の最初。B6b は実行していない。
鍵の値は画面、ログ、ToDo、記憶に出さない。

## 手順

1. 変更前の控えを作る。`~/.config/magpie/caller-keys.json` を撤去の archive（F-5.1）の `magpie/caller-keys.json.bak` へ `cp -p` する。既にあれば上書きしない。権限は 600。
2. `~/.local/bin/magpie gateway-key add ax` を実行する。`caller-keys.json` に項目 `ax` が足される。モデルは絞らない（S-8.4）。
3. 項目 `ax` の `secret` を Python で読み、`secrets_dir` の `model-gateway-key` へ直接書く（権限 600）。標準出力には出さない。
4. HTTP コードだけで確かめる。
   - hub-host のホストで、鍵つきの `GET /v1/models` が 200、鍵なしが 401。
   - AX のコンテナの網から `198.51.100.1:3425` へ同じ呼び出しが 200（`procedure-firewall.md` の ufw の規則の後）。
5. 失敗したとき（コマンドが無い、出力の形が違う）は、値を推測しない。ToDo カードで利用者に頼む。本文は「magpie のアプリの Gateway keys で名前 `ax` の鍵を作り、HQ の鍵タブの `model-gateway-key` に入れてください」だけにする。

## 回転と失効

- 回転は `magpie gateway-key rotate <id>`。回転したら手順 3 で書き直し、鍵を使うコンテナを `restart` する。
- 失効（`remove <id>`）は、利用者が個別に指示したときだけ行う。

## launcher との関係

launcher は `secrets_dir/model-gateway-key` の中身を `ANTHROPIC_AUTH_TOKEN` に入れて、`sudo -n --preserve-env=ANTHROPIC_AUTH_TOKEN docker run` を呼ぶ。コマンド行には値が出ない。
verify 用の runner（`kind: verify`）には鍵を渡さない。

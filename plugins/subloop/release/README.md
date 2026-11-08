# release サブループ

`release` は公開待ちを製品ごとにまとめる。待ち項目の一意キーは `repo` と `pr` の組である。バッチを確定すると、対象 PR と `target_sha` を固定する。実行中に届いた項目は次のバッチへ残す。

`config/products.yaml` は製品ごとの公開パス、版規則、ノートの言語、静穏時間、待ち上限、環境入口、確認項目を持つ。登録にない製品は公開しない。資格を確認できない製品は、その製品の公開だけを無効にする。

公開前に継続許可、固定 SHA の試験証拠、公開ゲート、製品固有の条件を確認する。Dreamforge は clean な `main` と `dev` の一致、fast-forward、通常版、prerelease と zip の確認を必要とする。Workshop はコンテンツと更新ノートだけを更新し、タイトル、説明、プレビューは更新しない。

既定は dry-run である。dry-run は予定とノートを作るが、公開待ち、タグ、公開、配備、Issue の完了を変更しない。外部操作は直前に `launching` を記録し、結果が不明なら再送しない。

hibi の GitHub Actions 公開は手順だけが定義済みで、環境入口は未定義である。そのため hibi の公開は `publication_contract_undefined` として保留する。

## 設定と運用

公開待ちの項目をまとめ、製品の登録情報に従って公開します。

manifest の `schedule` は `{"kind": "interval", "every_s": 300, "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `inbox_max` | `50` | 受け取り待ちの上限 |
| `dry_run` | `true` | 外部操作を予定だけにする |
| `every_s` | `300` | 巡回の間隔 |

manifest の秘密の識別子: `github-token`。これらは環境変数名ではありません。

登録後、制御の `run_now` で実行を要求できます。`set_mode` で `draining` を指定すると新しい実行を止められます。再開には対応する mode を指定します。制御要求の受付は実行完了を意味しません。

GitHub への公開などを扱います。`dry_run: true` が既定です。

試験: `tests/b15/release/`。

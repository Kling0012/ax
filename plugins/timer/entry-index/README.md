# entry-index

プラグインと Compose サービスの登録情報を集め、一覧を保存します。

## 設定と運用

manifest の `schedule` は `{"kind": "interval", "every_s": 3600, "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `every_s` | `3600` | interval |

manifest の秘密の識別子: なし。これらは環境変数名ではありません。

登録後、制御の `run_now` で実行を要求できます。`set_mode` で `draining` を指定すると新しい実行を止められます。再開には対応する mode を指定します。制御要求の受付は実行完了を意味しません。

収集処理は外部を一覧から除外します。

試験: `tests/b16a/`。

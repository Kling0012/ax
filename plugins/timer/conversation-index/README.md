# conversation-index

会話を伏せ字にして索引を作ります。

## 設定と運用

manifest の `schedule` は `{"kind": "cron", "cron": "41 19 * * *", "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `u_index_time` | `"04:41"` | 索引時刻 |
| `enabled` | `false` | 自動実行 |

manifest の秘密の識別子: なし。これらは環境変数名ではありません。

登録後、制御の `run_now` で実行を要求できます。`set_mode` で `draining` を指定すると新しい実行を止められます。再開には対応する mode を指定します。制御要求の受付は実行完了を意味しません。

外部埋め込みを使う構成では登録済みモデルへテキストを送ります。`enabled: false` が manifest の既定で、ローカル処理は外部索引を使いません。

試験: `tests/b16a_t60_t61/`。

ローカル索引の入力には `AX_CONVERSATION_HISTORY_DIR`、`AX_DATA_DIR`、`AX_REDACT_INDEX`、`AX_PII_LIST` を使います。

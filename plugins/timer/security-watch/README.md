# security-watch

セキュリティ監査を環境ジョブとして実行します。

## 設定と運用

manifest の `schedule` は `{"kind": "cron", "cron": "20 21 * * *", "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `u_security_time` | `"06:20"` | Audit time |

manifest の秘密の識別子: なし。これらは環境変数名ではありません。

登録後、制御の `run_now` で実行を要求できます。`set_mode` で `draining` を指定すると新しい実行を止められます。再開には対応する mode を指定します。制御要求の受付は実行完了を意味しません。

監査の submit と get を使います。通知先への送信は別に管理されます。

試験: `tests/b16a/`。

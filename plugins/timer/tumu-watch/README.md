# tumu-watch

Tumu の監視を設定されたタイマークライアントに依頼します。

## 設定と運用

manifest の `schedule` は `{"kind": "interval", "every_s": 900, "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `every_s` | `900` | interval |

manifest の秘密の識別子: なし。これらは環境変数名ではありません。

登録後は `collect_only` で巡回します。停止には制御の `set_mode` で `draining` を指定します。この manifest は `run` に対応していません。

manifest に外部公開の出力はなく、既定の mode は `collect_only` です。

試験: `tests/b16a/`。

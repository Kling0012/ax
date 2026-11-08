# change

Issue と PR を収集し、変更の検証とマージを進めます。

## 設定と運用

manifest の `schedule` は `{"kind": "interval", "every_s": 300, "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `dry_run` | `true` | 外部操作を予定だけにする |
| `max_parallel_items` | `1` | 同時に進める項目の上限 |
| `every_s` | `300` | 巡回の間隔 |
| `max_hops` | `20` | 項目の再入場の上限 |
| `change_starts_per_cycle` | `4` | 一巡で起動する項目の上限 |
| `change_model_calls_per_cycle` | `30` | 一巡のモデル呼出数の上限 |
| `change_cycle_seconds` | `300` | 一巡の時間の上限 |
| `change_fix_rounds` | `3` | 修正ラウンドの上限 |
| `test_cases_max` | `12` | 変更のテスト件数の上限 |

manifest の秘密の識別子: `github-token`。これらは環境変数名ではありません。

登録後、制御の `run_now` で実行を要求できます。`set_mode` で `draining` を指定すると新しい実行を止められます。再開には対応する mode を指定します。制御要求の受付は実行完了を意味しません。

GitHub へのコメント、PR 更新、マージ、push を扱います。`dry_run: true` が既定です。

試験: `tests/b12b3/core_change/`、`tests/change_ops/`。

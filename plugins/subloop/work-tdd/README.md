# work-tdd

標準のサブループ。実装を伴う依頼はすべてここで `tdd-change` を通る（S-7.6、S-7.10）。

- 項目の受け取り: 制御 API の中継（`POST /v1/plugins/work-tdd/items`）から `POST /items`。冪等キーは `item_key`。
- グラフ: `graph.yaml`。部分グラフは `plugins/subloop/_lib/tdd-change.yaml`。
- 結果の受け渡し: `push` が `ok` で `item.state(completed)`。ブランチは `work.git` だけ。ToDo の遷移は台帳が `item.state` を読んで行う。
- prompts は部分グラフ側（`plugins/subloop/_lib/prompts/tdd-change/`）。このサブループ固有の prompts が要るときは `prompts/` に置く。

## 設定と運用

実装依頼をテスト駆動の手順で進め、ブランチを用意します。

manifest の `schedule` は `{"kind": "interval", "every_s": 300, "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `max_parallel_items` | `1` | 同時に進める項目の数 |
| `inbox_max` | `50` | 受け取り待ちの上限 |
| `every_s` | `300` | 受け取り待ちを拾う間隔（秒） |
| `max_hops` | `20` | 1 項目の再入場の合計の上限 |
| `test_cases_max` | `12` | テストの件数の上限（1 件の変更あたり） |

manifest の秘密の識別子: なし。これらは環境変数名ではありません。

登録後、制御の `run_now` で実行を要求できます。`set_mode` で `draining` を指定すると新しい実行を止められます。再開には対応する mode を指定します。制御要求の受付は実行完了を意味しません。

manifest は `git.push` を出力として宣言します。実行には別途制御と許可が必要です。

試験: `tests/b7/`。

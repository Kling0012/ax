# B12-1 作業依頼と中核変更

## 実装

- `work.request` は ToDo を先に作り、実装役割を `POST /v1/plugins/work-tdd/items` へ渡す。
- 読み取り役割は worker-pool へ直接送る。作業ディレクトリを指定する値は受け付けない。
- `inbox_full` は ToDo を `parked` にし、1時間後の見直し時刻を付ける。
- `item.state` の完了を購読し、中核変更は検証、取り込み、`accepted/<sha>`、deploy 要求の順に進める。
- 承認、検証、取り込みのどこかで失敗した場合は、ブランチと ToDo を残し、deploy を要求しない。

## 未実装の境界

`schemas/dest/` の本体は別担当であり、現状の `work.request` は本文と受け入れ条件を表せない。`core.change` の payload も未確定で全拒否になる。このため公開ハンドラは推測で項目を作らず、501 を返す。`common.submit_item` と `core_change.execute_approved` は、正規化済みの項目および承認済みの payload を受け付ける。

## 検証

| 確認 | 結果 |
|---|---|
| `python3 -m py_compile` | 成功 |
| `python3 -m pytest -p no:cacheprovider tests/b12/work_core -q` | 33 passed。pytest-asyncio を自動読込しないため `asyncio_mode` の警告1件 |
| 実環境の work-tdd、worker-pool、verify-ax-ci、git、hostwatch | 未実行。偽の部品で通信と副作用を置換 |

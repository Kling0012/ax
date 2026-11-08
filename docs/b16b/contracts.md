# B16b 共通インターフェース

開始指示は `permission.change` と `control.command` を使う。行き先は増やさない。

## 参照先

| 用途 | ファイル |
|---|---|
| 送信対象の一覧 | `schemas/ext/b16b/delivery-targets.schema.json` |
| 許可の登録 | `schemas/ext/b16b/permission-change.schema.json` |
| 制御命令 | `schemas/ext/b16b/control-command.payload.schema.json` |
| 送信記録 | `schemas/ext/b16b/automation-delivery.schema.json` |
| 共通処理 | `core/intake/dest/b16b/common.py` |
| 送信状態の保存 | `core/intake/dest/b16b/delivery.py` |

## 送信対象

一覧は `{"targets": [{"plugin_id": "...", "project": "...", "repo": "...", "destination": "...", "operations": ["..."]}]}` とする。
`plugin_id` は登録簿の処理 ID に合わせる。宛先は設定済みの識別子を使う。X は送信先 ID、Discord は期待 channel ID を使う。操作名は X が `投稿`、Discord が `送信`、scheduled-publisher が `publish`、`reply`、`respond`、`friend_request`、`friend_accept` である。

制御 API は `AX_AUTOMATION_TARGETS` の JSON を毎回読み直す。既定は `/state/control/automation-targets.json`。ファイルが無い間は空の一覧とし、送信の開始を拒否する。旧設定や旧キューを読まない。実際の識別子の登録は B17 で行う。

## 許可と起動

許可は S-20.3 の形で登録する。`project`、`repo`、`destination`、`operations` の全体が一覧に一致する必要がある。空の値、ワイルドカード、未登録の操作を含む登録は全体を拒否する。
`source_instruction` は秘密を含まない利用者の指示の参照とする。

起動命令は `op: set_mode`、`args: {mode: run, permission_ids: [...]}` とする。`corr` に許可と同じ `source_instruction` を入れる。命令の `reason` は必須。入口は確認済みの利用者の操作だけを `issued_by=user` として中継する。この命令に期限は付けない。
司令塔の受け口からの開始指示は承認待ちになる。承認後の再実行は利用者の資格で行う。本文の自己申告で昇格しない。

## 送信前の照合

送信側は `PermissionReader(client).check(target, operation)` で既存の `POST /v1/permissions/check` を呼ぶ。呼べないときは送信を保留する。毎回読み直し、許可をキャッシュしない。許可の条件と公開ゲートは送信側でも検査する。
`DeliveryJournal.deliver(...)` は `mode`、登録済み候補、期待する宛先、許可、必須検査の順に確認する。拒否した候補のキーは作らない。`collect_only` では送信も送信状態の作成もしない。

## 送信記録

キーは `(plugin_id, destination, source_item_id)`。`DeliveryJournal` は SQLite に `launching` を先に保存し、同じキーの並行送信を拒否する。保存先は各プラグインの永続領域に置く。箱の中には置かない。
送信処理は成功時に外部結果 ID を返す。確実に未送信なら `NotSent` を返す例外、成否不明ならそれ以外の例外を送出する。空の結果 ID も成否不明とする。
成否不明は `unknown` とし、`lost` のカードを要求する。起動時に残っている `launching` も `unknown` にする。外部照合までは再送しない。
`automation.delivery` は `plugin_id`、`destination`、`source_item_id`、`operation`、`state`、`external_id`、`checked_at`、`reason` を持つ。本文、webhook、Cookie は含めない。

## 結合確認

3種類の送信処理はこの共通処理を使うか、同じ検査と状態遷移を満たす。各担当の実装がそろった後、実装側の呼び出しとスキーマ参照を確認する。共通処理の偽だけで各プラグインの結合完了とはしない。

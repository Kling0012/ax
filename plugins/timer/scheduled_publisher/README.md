# scheduled-publisher の巡回

S-23.7 の処理を、AX 側の状態と注入した部品で実行する。入口は `ScheduledPublisher.run(target, scheduled_at=...)`。対象は b16b-1 の `TargetRegistry` に登録し、モードと許可は制御 API から読む。

`collect_only` は対象と操作を `targets.json` に保存する。環境選択、箱、判断ワーカー、送信を呼ばない。`run` では収集、判断、検査、送信、記録の順に進む。各送信の直前に許可を引き直す。

## 状態と検査

生成先には AX の `reports/scheduled-publisher/` を渡す。箱の中の状態と旧の `sent.tsv` は読まない。SQLite に巡回の実行キー、送信のキー、当日の件数を算出する履歴、検索の既出 ID、ID の持ち主を保存する。送信前に共通の `DeliveryJournal` が `launching` を確定する。再起動時の `launching` は `unknown` にする。

`State.register_owner(env_id, user_confirmed=True)` は利用者による登録の接続口である。初回と箱の切替後は、probe 済みの箱の識別子を利用者が登録するまで巡回を保留する。ID の値は読まない。既に `sent` の本文と `unknown` の行は箱を替えても再送しない。

許可外の操作、長さ超過、許可外の URL、同じ本文の publish、同じ投稿への respond、24時間以内の同じ会話への reply を拒否する。reply は JST の1日5件、送信の総数は1巡12件が既定である。秘密・個人情報の検査には `axcommon.redact.Redactor` を渡す。送信する各本文と note は、注入した公開ゲートにも通す。ゲートの未接続や検査不能は送信を止める。

収集結果と検査結果は `<run_id>/` に保存する。保存前に秘密と個人情報を伏せる。報告は `<UTC>.md`、送信履歴の表示用の写しは `sent.tsv` に置く。`sent.tsv` の列は F-6.7 と同じ8列である。送信の判断は SQLite の記録を参照する。

## 部品の接続

`EnvironmentRunner` は資源の環境選択と `env.run` に接続する。要求には `requires: [linux-sandbox, scheduled-publisher-cli]`、`sensitivity: public` と登録済みの入口名だけを渡す。箱を名前で指名せず、ドライバを作り直さない。判断ワーカーには検査済みの収集結果、使ってよい事実、直近72時間の publish の冒頭、既出 ID、固定の指示だけを渡す。箱の接続先と資格を渡さない。

`Candidate`、`Decision`、`EnvResult` は内部の Python 型である。実際の CLI や共有 API の JSON Schema を定義するものではない。JSONL の保存形式も未確認の CLI へそのまま送ってはならない。

`manifest.yaml` と `graph.yaml` は `sl-scheduled-publisher` の6段を記述する。現時点では実行基盤への接続が未完了であり、このグラフを本番へ登録してはならない。`integration.bind_runtime()` と既定の `UnconfirmedCLI` は `NotImplementedError` を返す。CLI の引数と応答を推測して呼ぶ経路はない。

接続には次の確定が要る。

- `config/scheduled-publisher/` の登録簿の所有者、読み取りの許可一覧、事実と旧の6項目の報告書式。
- `eigen-collect`、`eigen-send` の引数 Schema、固定 argv、`launched` と行ごとの結果、外部 ID の照合方法。
- 共有の役割 Schema と `worker.run` への `eigen-decide` の追加、および Claude 系を選ばないモデル方針。
- `eigen.preflight`、`eigen.outbox_check`、`eigen.record` のノード登録、成果物の受け渡し、実効 knob の反映。
- S-23.7 の `feed_feedback` と S-23.8 の開始許可の操作一覧の食い違い。確定までは `operation_unresolved` で拒否する。

## 検証

担当試験は `tests/b16b/scheduled_publisher/` にある。`tests/fakes/b16b/scheduled_publisher/parts.py` の偽の Instabox と box、偽の CLI、制御部品、ワーカーだけを使う。偽の CLI の引数と応答は試験用であり、実際の CLI の確認結果ではない。pytest の共通 fixture がネットワーク接続を拒否する。

実環境でしか確認できない項目は次のとおりである。今回の作業ではいずれも未実行である。

| 試験 | 確認すること | 未実行の理由 |
|---|---|---|
| T-140 | 両箱の CLI の版、ID の置き場、probe、箱の交換後の持ち主 | 実機とサービスへの接続は作業範囲外 |
| T-139 の接続 | 実際の入口の引数、行ごとの `launched`、結果 ID、照合 | B18 と CLI の現物確認が必要 |
| T-63 の隔離 | 判断用コンテナから箱の経路と資格へ到達できないこと | コンテナの起動は禁止 |
| T-67 の統合 | 開始指示と権限取消から実送信までの制御 API の接続 | 本番への接続と `eigen-decide` の登録が未完了 |

## 設定と運用

収集した対象について判断し、許可された操作を実行します。

manifest の `schedule` は `{"kind": "interval", "every_s": 3600, "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `u_eigen_interval_s` | `3600` | 巡回の間隔 |
| `u_eigen_collect_budget_min` | `15` | 収集の上限時間 |
| `u_eigen_decide_budget_min` | `40` | 判断の上限時間 |
| `u_eigen_send_budget_min` | `10` | 送信の上限時間 |
| `u_eigen_run_send_max` | `12` | 1巡の送信の上限 |
| `u_eigen_reply_cap_day` | `5` | 1日の返信の上限 |

manifest の秘密の識別子: なし。これらは環境変数名ではありません。

登録後、制御の `run_now` で実行を要求できます。`set_mode` で `draining` を指定すると新しい実行を止められます。再開には対応する mode を指定します。制御要求の受付は実行完了を意味しません。

投稿、返信などの送信があります。既定の mode は `collect_only` で、収集のみです。

試験: `tests/b16b/scheduled_publisher/`。

# HQ API

HQ の業務 API は認証済みの利用者だけに提供する。書き込みは `IntakeClient` から入口へ渡す。台帳と worker-pool を直接変更しない。

## 共通の応答

照会の成功時は `ok`、`data`、`sample`、`last_success_at` を返す。取得に失敗したときは `data: null` と `error.code` を返す。以前の値を現在値や見本として返さない。

書き込みは `op_id` と `payload` を受ける。成功の `receipt` は入口の受付結果であり、状態の変更確認ではない。入力は失敗時もクライアント側で保持する。

`hq_sample` または `sample_mode` のとき、すべての書き込みは `403 sample_read_only` で拒否する。

## 依頼と ToDo

| API | 説明 |
|---|---|
| `POST /v1/requests` | `{op_id, payload}` を `work.request` として入口へ渡す。入口結果に ToDo ID があれば `todo_id`、`todo_ref`、`todo_api` を返す。 |
| `POST /v1/todos/{todo_id}/answer` | `{op_id, answer, quote?, version?}` を `ledger.answer` として入口へ渡す。同期受付後は ToDo を読み直し、`todo` に戻ったことを確認する。 |
| `POST /v1/reports/unfinished/check` | `{items: [{todo_id}]}` の各 ToDo が未完了かを照会する。存在しない ID、完了済み ID、取得失敗は `ok: false` と理由で返す。 |

同じ `op_id` は同じ入口の冪等キーになる。入口側が再送を一度だけ処理する。

## 照会

| API | クエリ | 説明 |
|---|---|---|
| `GET /v1/todos` | `state`、`project`、`subloop`、`owner`、`due` | ToDo 一覧 |
| `GET /v1/todos/{id}` | なし | ToDo と履歴 |
| `GET /v1/goals`、`/v1/promises`、`/v1/schedules` | 台帳で定めた絞り込み | 一覧 |
| `GET /v1/goals/{id}`、`/v1/promises/{id}`、`/v1/schedules/{id}` | なし | 対象と履歴 |
| `GET /v1/focus-state` | なし | 集中状態 |
| `GET /v1/daily-state/{date}` | なし | 日次状態 |
| `GET /v1/digests/{date}` | なし | 保存済みの朝の要約と配送状態 |

朝の要約を取得できないときは `503 digest_unavailable` を返す。空の要約を成功として返さない。

## 未確定の変更操作

`POST /v1/operations/{destination}` は `work.stop`、`work.cancel`、`work.resume`、`schedule.update`、`life_state.update`、`goal.update`、`commitment.update` の入口だけを受け付ける。

これらの payload は設計文書で対象 ID、操作、理由、成果物、再開参照の形が未確定である。対応する Schema が未解決の間は `501 contract_unresolved` を返し、入口・台帳・worker-pool を変更しない。

## Hibi

Hibi の経路は `GET /v1/hibi/now` と `GET /v1/hibi/day?date=YYYY-MM-DD` だけである。tailnet 直接入口で許可された利用者だけが読める。公開入口、許可外、接続元を判定できない要求は `403 local_only` で拒否する。`GET` 以外は `405` と `Allow: GET` を返す。

`now` は `state`、`since`、`category`、`session_title`、`as_of` を返す。`day` は `date`、`tz`、`as_of`、`segments` を返す。予定、ToDo、目標、約束、ノートは返さない。

台帳の値を取得できない、形式が違う、または `as_of` が `u_hibi_interval_s` の3倍より古いときは、現在の値を `state: unknown` として返す。古い値を現在値として返さない。日次状態も `state: unknown` と空の `segments` を返す。

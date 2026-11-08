この文書は中核と作業側の部品の HTTP 口と健康の見方をまとめたものです。
コードは `core/` の下と `plugins/resource/worker-pool` にあります。
入口（`intake`）、関門（`gate`）、HQ の口は別の文書（`api-intake-gate-hq.md`）で扱います。

> 概要（3行）
> 時系列記録と台帳と制御 API は、各部品の 8080 の中核の口と 8081 の作業側の口を持ちます。資源と記憶コネクタと worker-pool も同じ2つの口を持ち、通す項目は宣言した物だけです。オブザーバーと点検係と管理役とアーカイバは読み取りだけの口か、健康の口だけを持ちます。

## 目次

- [1. 網と口の基本](#1-網と口の基本)
- [2. 認証情報](#2-認証情報)
- [3. 共通の応答と錯誤](#3-共通の応答と錯誤)
- [4. 健康の見方（`GET /healthz`）](#4-健康の見方get-healthz)
- [5. `timeline`（時系列記録）](#5-timeline時系列記録)
- [6. `ledger`（台帳）](#6-ledger台帳)
- [7. `control`（制御 API）](#7-control制御-api)
- [8. `resources`（資源台帳）](#8-resources資源台帳)
- [9. `worker-pool`（ワーカーのジョブ）](#9-worker-poolワーカーのジョブ)
- [10. `memory-connector`（記憶コネクタ）](#10-memory-connector記憶コネクタ)
- [11. `observer`（見張り）](#11-observer見張り)
- [12. `inspector`（点検係）](#12-inspector点検係)
- [13. `manager`（管理役）](#13-manager管理役)
- [14. `archiver`（退避）](#14-archiver退避)
- [15. `egress-proxy`（外向きの中継）](#15-egress-proxy外向きの中継)
- [16. 事象](#16-事象)
- [17. JSON Schema の索引](#17-json-schema-の索引)
- [18. 状態](#18-状態)

## 1. 網と口の基本

AX は docker compose の網を4つに分けます。部品は網への所属を決められていて（`compose.yaml` の `networks` と `compose.d/*.yaml`）、口の開け方は用途で決まります。

| 網 | 意味 | 備考 |
|---|---|---|
| `ax-core` | 中核の網 | 中核の部品はここに居ます |
| `ax-work` | 作業の網 | サブループとワーカーはここに居ます |
| `ax-publish-review` | 公開前レビュー用の網 | `internal: true`。公開前レビューを隔離します |
| `ax-job` | ジョブの網 | `internal: true`。外へ直接出られません |

2つの口で待つ部品と、1つの口で待つ部品があります。2つの口を持つ部品は、1つのプロセスが 8080 と 8081 の両方を開きます。8080 は `ax-core` のアドレス（健康の検査用に `127.0.0.1` を足す）、8081 は `ax-work` のアドレスだけに結び付けます（`core/timeline/netbind.py`）。1つの口しか持たない部品は、その網のアドレスの 8080 だけを開きます。`archiver` は `ax-core` の 8080 だけです。`egress-proxy` は `ax-work`（固定アドレス `.2`）と `ax-job` の両方に足を持つ唯一のコンテナです。

`/healthz` は、全部の部品が同じ形を返します（`axcommon/http.py` の `create_app`）。`GET /healthz` は `{"ok":true,"component":"<部品の名>","version":"<版>"}` を返します。追加の項目があるとき（`extra_health`）や、拡張の読み込みに失敗した名（`ext_failed`）を足します。コンテナの健康の検査は `docker/base.yaml` の `axcommon.healthcheck` が `http://127.0.0.1:${AX_HEALTH_PORT}/healthz` を叩きます（`AX_HEALTH_PORT` の既定は 8080）。

別の網に居るコンテナは物理的に届かないため、外の経路で中核の口へ届く通信は起こりません。作業の網からは中核の口へは届きません。作業側の口（8081）の API 以外は、`work_not_found` が 404 を返します。

## 2. 認証情報

| 資格 | 作り方 | 判定場所 | 使われる口 |
|---|---|---|---|
| プラグインのトークン | `host/ax-up.sh` が `${secrets_dir}/plugin-tokens/<id>` に 32 バイトの乱数の 16 進を作ります | 値は共有フォルダの `plugin-tokens.sha256` のハッシュ一覧に入り、作業側の口は `Authorization: Bearer <トークン>` のハッシュで呼び出し元のプラグイン ID を切り出します（`core/ledger/workauth.py` の `plugin_table`） | 作業側の口（8081）。一覧に無ければ 401 です |
| 中核の資格（`core-tokens`） | `${secrets_dir}/core-tokens/<部品名>` に同じ形で作ります | `${ax_state_dir}/core-tokens.sha256` のハッシュだけを持ちます。1 行が `<名前> <sha256>` です。資格が無い API は 401、資格があっても範囲外は 403（`core/resources/core_auth.py` の `CoreAuth.check`） | 中核の口（8080）の書き込み系 |
| 制御の専用トークン | compose の `secrets`（`control-token-manager` など） | `core/control/callers.py` の `issuer_for` が決めます。本文の `issued_by` は信用せず、資格と照合だけします | 中核の口（8080）の書き込み系 |

秘密の値はログや例外や応答へ出しません。比較は定数時間で行います（`axcommon.creds.TokenTable.identify`）。

## 3. 共通の応答と錯誤

### 錯誤の形

エラー応答は `{"error":{"code":"<コード>","message":"<説明>"}}` の形で、ときには `hint` が付きます（`schemas/api-error.schema.json`）。例外の詳細（スタックなど）は返しません。未処理の例外は `500 {"error":{"code":"internal","message":"内部エラー"}}` です（`axcommon/http.py`）。

### 錯誤コード

HTTP の状態コードと組み合わせるコードです。

| 状態 | コード | 意味 |
|---|---|---|
| 400 | `bad_json` | 本文が JSON でない |
| 400 | `bad_request` | 本文の形が正しくない（スキーマの検査で出ます） |
| 400 | `issuer_mismatch` | 本文の `issued_by` が呼び出し元の資格と違う（制御の口） |
| 400 | `pin_model_not_allowed` | `pin_model` を含むジョブ依頼（worker-pool の項を参照） |
| 401 | `unauthorized` | 資格が無いか一覧に無い |
| 401 | `unauthenticated` | 資格が一覧に無い（`resources` の `CoreAuth` の表記） |
| 403 | `forbidden` | 呼び出し元の資格はあるが、この操作はできない |
| 403 | `credential_scope` | 資格がこの API の範囲外 |
| 403 | `component_mismatch` | `source.component` が呼び出し元と違う（`timeline` の作業側の口） |
| 403 | `reserved_field` | `principal` / `session` / `via` を本文で渡した（同じく作業側の口） |
| 403 | `kind_not_allowed` | 書いてはいけない `kind` を書いた（同じく作業側の口） |
| 403 | `plugin_not_allowed` | `hibi-state` 以外のプラグインが集中状態・日次状態を書いた（`ledger` の作業側の口） |
| 404 | `not_found` | 対象が無い、または作業側の口に用途が無い API |
| 409 | `already_exists` / `already_answered` / `invalid_transition` など | 既にある物との競合、または遷移の表にない遷移 |
| 413 | `too_large` | 本文が大きすぎる |
| 422 | `bad_request` / `schema` | 本文の形が正しくない（`resource` の `read_json` の既定） |
| 429 | `rate_limited` | 頻度の上限です |
| 501 | `not_implemented` / `timer_transport_undefined` など | 未実装の口（§7 と §12 を参照） |
| 503 | `unavailable` | 外部へ届かない、または外部に接続先が無い |
| 500 | `internal` | 内部エラーです |

### 実装メモ

この実装はエラー応答を `schemas/api-error.schema.json` に合わせるよう書き方を寄せています。実装が無い部分（501 の口など）は、各部品の章にまとめます。

## 4. 健康の見方（`GET /healthz`）

全部の部品は `GET /healthz` を持ちます。初めの形は `{"ok":true,"component":"<部品名>","version":"<版>"}` （スキーマは `schemas/healthz.schema.json`）で、部品ごとの追加項目はそれぞれの項に書きます。

健康の検査は、`docker/base.yaml` の `axcommon.healthcheck` が 8080 の `GET /healthz` を叩き、`ok` が `true` なら成功とします。起動の開始時に、心拍の間隔（既定 60 秒）で `health.beat` の事象を時系列記録へ送ります。

部品ごとの版と追加項目は次のとおりです。

| 部品 | `component` / 版 | `GET /healthz` に足す物 |
|---|---|---|
| `control` | `control` · 1.0.0 （`core/control/__init__.py`） | `plugins`（登録の数）と `graph_check`（`tools/ax_graph_check.py` が使えるか） |
| `control`（作業側の口） | `control-work` · 1.0.0 | `mouth` = `work` |
| `ledger` | `ledger` · 1.0.0 | `todos`（ToDo の数）、`secret_scan`、`cursor_item_state` |
| `ledger`（作業側の口） | `ledger-work` · 1.0.0 | `mouth` = `work` |
| `timeline` | `timeline` · 0.1.0 | `projection_skipped`、`last_seq`、`redact_index`、`redacted_rejected_total` |
| `timeline`（作業側の口） | `timeline` · 0.1.0 | `plugin_tokens`（トークンの一覧があるか） |
| `resources` | `resources` · 0.1.0 | `seeded`、`seed_error`、`core_tokens`（ハッシュの表の名前）、`table_ok` |
| `resources`（作業側の口） | `resources` · 0.1.0 | `resources_open`（`true`） |
| `worker-pool` | `worker-pool` · 0.1.0 | `jobs`（ジョブがあれば `true`） |
| `worker-pool`（作業側の口） | `worker-pool` · 0.1.0 | `jobs_open`（`true`） |
| `memory-connector` | `memory-connector` · 0.1.0 | `memory_endpoint`、`queued`、`redact_index`、`counts`（`saved` / `superseded` / `duplicate` / `queued` / `unknown` / `rejected` の数） |
| `memory-connector`（作業側の口） | `memory-connector` · 0.1.0 | `plugin_tokens` |
| `observer` | `observer` · 0.1.0 | `runs`、`last_run_at`、`timeline_down`、`alerts_total`、`unimplemented_checks`、`failed`（検査ごとの失敗の数）、`hooks` |
| `inspector` | `inspector` · 0.1.0 | `phase`、`next_at` |
| `manager` | `manager` · 0.1.0 | `cycles`、`last_cycle_at`、`timeline_ok` |
| `archiver` | `archiver` · 0.1.0 | `last_run_at`、`last_failed`、`runs`、`reap_enabled` |
| `egress-proxy` | `egress-proxy` · 0.1.0 | `GET /healthz` は `{"ok":true,"component":"egress-proxy","version":"0.1.0"}` を返します（`AX_HEALTH_PORT`、既定 8080）。他は 404 です |

## 5. `timeline`（時系列記録）

### 役割

全部の部品の記録を集める時系列の保存庫です。事象の追記・読み取り・表示用のデータ・JSON Lines への書き出しを行います（`core/timeline/api.py`）。

### 口

| 口 | 場所 | 方法 | 認証 |
|---|---|---|---|
| 中核の口 | `ax-core` の 8080 | — | 入口の保証がある `principal` / `session` / `via` を値に持つ事象を入れることができます |
| 作業側の口 | `ax-work` の 8081 | — | プラグインのトークンが要ります。一覧が設定されていない間は全部 401 です |

### 中核の口（8080）の経路

| 方法 | パス | 用途 | 本文・応答のスキーマ |
|---|---|---|---|
| POST | `/v1/events` | 1 件か配列で事象を追記します。`id` が既にあれば無視して成功です（1 回の追記は 1000 件まで） | 本文は `schemas/event-post.schema.json`、事象の形は `schemas/event.schema.json`、応答は `schemas/ext/b1/events-post-response.schema.json` |
| GET | `/v1/events` | `after`、`kind`（カンマ区切り可）、`subloop`、`project`、`subject`、`limit`（既定 200、上限 1000）、`wait`（長い待ち。上限は設定 `longpoll_s`）で読み取ります | 応答は `schemas/ext/b1/events-list-response.schema.json` |
| GET | `/v1/events/{event_id}` | 1 件の事象を読み取ります | 応答は `schemas/event.schema.json` |
| GET | `/v1/projections/{name}` | 表示用のデータ（`plugins` / `runs` / `jobs` / `controls` / `health`。ほかの名は 404）を返します | 応答は `schemas/projection-response.schema.json` |
| POST | `/v1/export` | 範囲を指定して JSON Lines に書き出します（退避コネクタが `archive/` から移します） | 本文は `schemas/ext/b1/export-request.schema.json`、応答は `schemas/ext/b1/export-response.schema.json` |

更新・削除の経路（`PUT` / `PATCH` / `DELETE`）はありません。事象の形の検査では `kind` ごとの `data` の条件は見ません。投影の必須の項目が無い事象も追記は成功させ、投影だけ飛ばす（`fail-soft`）仕組みです。

### 作業側の口（8081）の経路

| 方法 | パス | 制限 |
|---|---|---|
| POST | `/v1/events` | 資格が要ります。`source.component` はプラグイン ID と同じでなければ 403（`component_mismatch`）。`kind` は `kinds.WORK_SIDE_KINDS` か、そのプラグインの manifest が宣言した物だけ（違えば 403 の `kind_not_allowed`）。`principal` / `session` / `via` は本文から受け付けず、入っていれば 403 の `reserved_field` です。拒否したときは `events.rejected` を書きます |
| GET | `/v1/events` | 資格が要ります |
| GET | `/v1/events/{event_id}` | 資格が要ります |

書いてよい `kind` の一覧は `core/timeline/kinds.py` の `WORK_SIDE_KINDS` です。代表的なものは `run.state`、`item.state`、`node.state`、`resource.repeat_failure`、`error.classified`、`health.beat`、`change.cycle`、`change.route`、`release.pending`、`release.batch`、`release.result`、`publish.check`、`ci.collection`、`lesson.candidate`、`lesson.selected`、`question.notified`、`notification.failed`、`digest.generated`、`work.cancelled`、`work.resumed`、`safety.rejected`、`automation.observed`、`automation.delivery`、`automation.collect_checked` です。

### 事象を出す物

この部品自体も時系列記録へ事象を書き込みます。自分自身の事象（`health.beat`、`events.rejected`、`health.alert`）を同じプロセスの保存庫へ直接書き込みます（`core/timeline/api.py` の `StoreSink`）。

## 6. `ledger`（台帳）

### 役割

ToDo の台帳・遷移・依存・カード（S-9.1〜S-9.3）、目標・約束・予定・集中状態・日次状態（S-9.4、S-9.5）、判断の記録（S-9.6、D-8）を 1 つの SQLite に持ちます（`core/ledger/__init__.py`）。

### 口

| 口 | 場所 | 方法 |
|---|---|---|
| 中核の口 | `ax-core` の 8080 | 中核の資格、または `X-AX-Via` の受け口（`hq` が確かめた値だけ信用する）の読み取りと書き込み |
| 作業側の口 | `ax-work` の 8081 | プラグインのトークン。通す API は下の表の通り |

2 つの口は同じ処理の 2 つのアプリ（`core/ledger/app.py` の `create_core_app` / `create_work_app`）で、1 つのプロセスが両方の口を開きます。

### 中核の口（8080）の経路

| 方法 | パス | 認証 | 本文・応答のスキーマ |
|---|---|---|---|
| POST | `/v1/todos` | 中核の資格。201 を返します | `schemas/todo.schema.json` |
| GET | `/v1/todos` | — | `state`、`project`、`subloop`、`due`、`owner` で絞り込むことができ、応答は `{"todos": [...]}` です |
| GET | `/v1/todos/{todo_id}` | — | `?history=1` で履歴を付けます |
| POST | `/v1/todos/{todo_id}/transition` | 中核の資格 | `schemas/todo-transition.schema.json` |
| POST | `/v1/todos/{todo_id}/answer` | 中核の資格 | `schemas/todo-answer.schema.json` |
| GET | `/v1/focus-state` | `hq` か `commander` の資格（`STATE_CORE_CALLERS`） | — |
| PUT | `/v1/focus-state` | 同上 | `schemas/focus-state.schema.json`、応答は `schemas/state-put-response.schema.json` |
| GET | `/v1/daily-state/{date}` | 同上 | — |
| PUT | `/v1/daily-state/{date}` | 同上 | `schemas/daily-state.schema.json`、応答は `schemas/state-put-response.schema.json` |
| GET | `/v1/state-history/{kind}` | `hq` か `commander` の資格 | `kind` は `focus` か `daily` です |
| POST | `/v1/decisions` | 中核の資格。201 を返します | `schemas/ext/b3/decision.schema.json`（台帳の拡張スキーマ） |
| GET | `/v1/decisions` | — | — |
| GET | `/v1/decisions/{decision_id}` | — | — |
| POST/GET/PATCH | `/v1/goals`、`/v1/promises`、`/v1/schedules`（と `/{item_id}`、`/transition`） | 書き込みは中核の資格 | `schemas/ext/b3/goal.schema.json` / `promise.schema.json` / `schedule.schema.json` |

ToDo の遷移の表（`core/ledger/__init__.py` の `TRANSITIONS`）にない遷移は 409 です。`waiting_user` には理由（7 種のどれか）と質問のカードが要ります。`user_only=true` になる理由は固定です。`user_only` のカードは、入口の受け口（`hq` 以外）からは完了・保留・中止できません。

`item.state` を読んで対応する ToDo を遷移させる常時監視（`core/ledger/watch.py`）が、`GET /v1/events?kind=item.state&wait=` で購読しており、5 分周期で `check_at` を過ぎた `parked` へ `task.state`（`data.due=true`）を書きます。

### 作業側の口（8081）の経路

| 方法 | パス | 制限 |
|---|---|---|
| POST | `/v1/todos` | プラグインのトークン。作成者はプラグイン ID に固定する（本文の値は信用しない） |
| GET | `/v1/todos` | プラグインのトークン |
| GET | `/v1/todos/{todo_id}` | プラグインのトークン |
| PUT/GET | `/v1/focus-state`、`/v1/daily-state/{date}` | 呼び出し元が `hibi-state` のときだけ（ほかのプラグインは 403 の `plugin_not_allowed`）。発行者は `plugin:hibi-state` に固定する |

それ以外の経路は全部 404 です（遷移・回答・目標・約束・予定・判断の記録）。

### 出す事象

| 種類 | 意味 |
|---|---|
| `task.state` | ToDo の作成・遷移・回答・保留の走査の記録（`data` に `todo_id`、`state`、`reason`） |
| `state.recorded` | 集中状態・日次状態が変わった書き込みだけ出す記録 |
| `decision.recorded` | 判断の記録（`data` に `decision_id`、`kind`、`attested`） |
| `error.classified` | 依存の連鎖が深すぎた場合の分類（`code` = `dependency_chain_too_long`） |

## 7. `control`（制御 API）

### 役割

プラグインの登録簿（S-4.1、S-4.2）、制御命令の検証・適用・期限管理（S-5.3〜S-5.6、D-4.2、D-4.3）、継続許可（S-5.7、S-20.3）、タイマーの登録と期限（S-7.8、D-4.4）、検査の口 `POST /v1/validate`（S-5.7）を扱います（`core/control/__init__.py`）。

### 口

| 口 | 場所 | 方法 |
|---|---|---|
| 中核の口 | `ax-core` の 8080 | 中核の専用トークンか、入口の資格（`intake`）で呼びます |
| 作業側の口 | `ax-work` の 8081 | プラグインのトークン。読み取り 3 種類だけ |

### 中核の口（8080）の経路

| 方法 | パス | 認証 | 本文・応答のスキーマ |
|---|---|---|---|
| GET | `/v1/plugins` | — | 一覧です（`manifest` は除きます） |
| GET | `/v1/plugins/{id}` | — | 応答は登録の表示（`id`、`kind`、`status`、`note`、`revision`、`mode`、`knobs`、`manifest`、`registered_at`） |
| GET | `/v1/plugins/{id}/effective` | — | 生きている命令を当てていき、強い優先順位（`emergency` > `user` > `inspection` > `commander` > `manager` / `backup`）の物から選ぶ、宣言の値との組み合わせ |
| POST | `/v1/commands` | 呼び出し元の資格。201 です | `schemas/control-command-request.schema.json`、応答と保存は `schemas/control-command.schema.json` |
| GET | `/v1/commands` | — | `?state=` は `active` / `expired_pending` / `released`、`issued_by=` で絞ります |
| POST | `/v1/commands/{id}/release` | 呼び出し元の資格 | 解除した者は出した者、`user`、`emergency` だけです |
| POST | `/v1/commands/{id}/renew` | 呼び出し元の資格 | 延長先の `expires_at` と理由が要ります |
| GET | `/v1/permissions` | — | 一覧です |
| POST | `/v1/permissions` | `user` の資格。201 です | 許可の作成。`operations` は重複のない文字列の一覧で、日常の操作種類は入れません |
| POST | `/v1/permissions/check` | — | `schemas/permission-check.schema.json`（`/$defs/request` と `/$defs/response`） |
| POST | `/v1/permissions/{id}/disable` | `user` の資格 | 有効化を解除します |
| POST | `/v1/plugins/{id}/items` | 入口（`intake`）の資格だけ | 項目をサブループへ渡す中継です。中継は項目を保存せず、`sl-<id>` の `POST /items` へ渡して応答をそのまま返します（受付 `202` か `409`） |
| POST | `/v1/validate` | — | `schemas/validate.schema.json`（`/$defs/request` と `/$defs/response`） |
| POST | `/v1/timers` | `manager` の資格。201 です | タイマーの登録。判定は下の表のとおり |
| GET | `/v1/timers` | — | `?state=` は `active` / `fired` / `released` / `expired` です |
| GET | `/v1/timers/{id}` | — | 1 件です |
| POST | `/v1/timers/{id}/fire` | — | 501 です（`timer_transport_undefined`） |
| POST | `/v1/sweep` | 呼び出し元の資格 | 期限の走査を 1 回だけ手で回します（常時は 10 秒ごと） |

制御命令の保存は、`POST /v1/commands` の本文は `core/control/service.py` の `issue` で `schemas/control-command-request.schema.json` を読み、保存される命令は `schemas/control-command.schema.json` で検査します。`op` がこの実装で通るもの（`set_mode` / `set_knob` / `exclude_resource`）以外は 501（`operation_not_implemented`）です。期限を過ぎた命令は自動では無効にせず、`expired_pending` にして効力を残します（S-5.6）。

タイマーの登録は `POST /v1/timers`（`issued_by` が `manager` のときだけ）で行い、`at` と `every_s` の片方だけが要ります。`action` は `ledger.recheck` / `control.release` / `plugin.run_now` のどれかです。期限側のタイマーは制御の口が担うが、実際の起動は timer-runner（B7）が行います。

### 作業側の口（8081）の経路

読み取りだけです（プラグインのトークンが要ります）。

| 方法 | パス |
|---|---|
| GET | `/v1/plugins` |
| GET | `/v1/plugins/{id}` |
| GET | `/v1/plugins/{id}/effective` |

それ以外は 404 です。

### 出す事象

| 種類 | 意味 |
|---|---|
| `plugin.registered` | 起動時の登録の成功（`manifest_version` と `mode`） |
| `control.issued` | 命令の発行 |
| `control.applied` | 命令を対象へ渡したとき |
| `control.expired_pending` | 期限を過ぎた命令を `expired_pending` にしたとき |
| `control.released` | 命令を解除したとき |
| `control.renewed` | 命令を延長したとき |
| `control.rejected` | 検証に外れた命令を拒否したとき |
| `permission.checked` | 許可の照合の結果 |
| `permission.changed` | 許可の作成・無効化 |

### 未実装

- `POST /v1/timers/{id}/fire` は 501（`timer_transport_undefined`）です。発火報告の到達経路と資格が未定義です。
- 期限後のタイマーの発火（`ledger.recheck` と `plugin.run_now` の実行）は timer-runner の側です。

## 8. `resources`（資源台帳）

### 役割

資源の状態（`status`、`until`、`level`、`stats`）を持つ部品です。選ぶのは `worker-pool` ですが、選ぶのに要る資源の読み取りと、`worker-pool` が頼む書き込み（`outcome`、`exclude`、`prefix_cache`）を受け付けるのはここです（`core/resources/__init__.py`）。

### 口

| 口 | 場所 | 方法 |
|---|---|---|
| 中核の口 | `ax-core` の 8080 | 一部の API は `core-tokens` の資格だけ（`GET /v1/envs/select` の `path=G` など）。資格が無い API は 401、資格があっても範囲外は 403（`credential_scope`） |
| 作業側の口 | `ax-work` の 8081 | 宣言した API だけなど |

### 中核の口（8080）の経路

| 方法 | パス | 認証 | スキーマ |
|---|---|---|---|
| GET | `/v1/resources` | — | `kind`、`status`、`family`、`tier` で絞ります。資源の形は `schemas/resource.schema.json` です |
| GET | `/v1/resources/{id}` | — | 同上 |
| POST | `/v1/resources/{id}/outcome` | — | worker-pool の成績の書き込み。有効な `outcome`、`verify_rc`、`error_class`、`counts_as_failure`、`job_id`、`trial_id` を受け取ります |
| POST | `/v1/resources/{id}/exclude` | — | 期限付きの一時除外（`reason` は `rate_limit` / `quota` / `connect` / `capability` / `manual` / `periodic_check` のどれかです） |
| POST | `/v1/resources/{id}/include` | — | 一時除外の解除です |
| POST | `/v1/resources/{id}/prefix_cache` | worker-pool の資格だけ（`core-tokens` で検証） | `value` は `on` / `off` / `unknown` のどれかです |
| GET | `/v1/ledger` | — | モデルごとの実績（`model` / `role` / `since` で絞ります） |
| GET | `/v1/models/discovery` | — | 提供一覧と登録簿の差 |
| GET | `/v1/selections` | — | 選択記録（`?job_id=` で絞ります） |
| GET | `/status` | — | 状態（`{"mode": "run", "knobs": {}, "running": [], "queued": 0}`） |
| POST | `/apply` | — | 設定の反映（`{"ok": true, "ignored": false}`） |

`GET /v1/envs/select` と `POST /v1/envs/{id}/gate-token` / `/revoke` の 3 つの経路は、拡張（`core/resources/ext/b18b.py` の `register`）が動的に追加する経路です。ある場合は中核の口の資格で呼び、中核の部品からだけ呼び出す書き方が正しい経路です。`POST /v1/envs/{id}/gate-token` と `/revoke` は worker-pool の資格だけが呼べるもので、現在の実装は未実装（501）です。資格と項目の検査は通すが、本体の発行は `core/resources/envs/` の実装が `register` します。

### 作業側の口（8081）の経路

| 方法 | パス |
|---|---|
| GET | `/v1/resources` |
| GET | `/v1/resources/{id}` |
| GET | `/status` |

## 9. `worker-pool`（ワーカーのジョブ）

### 役割

プラグインの実体です。ワーカーのジョブを受け、役割に合うモデルを選び、同時実行数を守って起動、監視、検証し、結果を返します（`plugins/resource/worker-pool/__init__.py`）。

### 口

| 口 | 場所 | 方法 |
|---|---|---|
| 中核の口 | `ax-core` の 8080 | 資格なし（中核の口） |
| 作業側の口 | `ax-work` の 8081 | 宣言した API だけ |

### 中核の口（8080）の経路

| 方法 | パス | 用途 | スキーマ |
|---|---|---|---|
| POST | `/v1/jobs` | ジョブの依頼。`pin_model` を含む本文は 400（`pin_model_not_allowed`）で拒否します。`role`、`prompt`、`sensitivity` が要ります | `schemas/job-request.schema.json` |
| GET | `/v1/jobs/{id}` | 状態と結果 | `schemas/job.schema.json` |
| GET | `/v1/jobs` | 待機・実行・完了済みの一覧（`state` / `role` の絞り込み。200 件まで） | — |
| POST | `/v1/jobs/{id}/cancel` | 指定ジョブだけを取消します | — |
| GET | `/v1/slots` | 系統・モデルごとの使用数と上限 | — |
| GET | `/status` | 状態（`mode` / `knobs` / `running` / `queued`） | — |
| POST | `/apply` | 設定の反映（`{"ok": true}`） | — |

受付は 202 です。同じ役割のキューが一杯（5 件）のときは、`{"job_id": null, "state": "skipped", "reason": "queue_full"}` を 200 で返します。

ジョブの入力の検査（`validate_job_body`）は HTTP 層で手書きです。`pin_model` は内部の関数（`submit_internal`）だけが受けられ、HTTP では拒否します。`tools` は `research` / `screen` / `assist` の役割で、`sensitivity` が `public` のときだけ持てます。役割は `summary`、`research`、`second_opinion`、`screen`、`assist`、`test_design`、`impl`、`impl_hard`、`review`、`review_final`、`manager`、`backup_manager`、`impl_public` の 13 種類です。

### 作業側の口（8081）の経路

| 方法 | パス |
|---|---|
| POST | `/v1/jobs` |
| GET | `/v1/jobs` |
| GET | `/v1/jobs/{job_id}` |
| POST | `/v1/jobs/{job_id}/cancel` |
| GET | `/status` |

### 起動時の経路

`GET /v1/resources/{id}` と `POST /v1/resources/{id}/outcome` / `exclude` / `prefix_cache` は、`worker-pool` から `resources` の中核の口（8080）を呼ぶ経路です（`ax-core` の足から呼ぶ）。

### ジョブの状態

ジョブは次の順に進みます（`plugins/resource/worker-pool/jobs.py`）。

`queued` → `launching` → `submitted` → `running` → `ok` / `failed` / `timed_out` / `cancelled` / `lost` / `rejected`

立つ作業は `launching` / `submitted` / `running` を数えて枠を数え出します。

### 出す事象

worker-pool の現行の実装は `sink` を持たず、事象を自ら出しません（`plugins/resource/worker-pool/api.py` の `PoolService.sink` は `None` を返し、`app.py` の `sink` は `None` です）。ジョブの状態の記録（`job.state`）は、時系列記録の投影（`core/timeline/projectors.py`）が読みますが、今の経路では worker-pool 自身は事象を書きません。

モデルの選択(`worker.selection`）と利用量（`worker.usage`）の事象は、`usage.py` の `event_data` で形は決まっていますが、今は呼び出していないため、事象は出しません。`env.usage` は `core/resources/box/box_mcp.py` の箱の利用量が、`sandbox.call` と `env.machine_clock` を添えて出す事象です。`env.selection` は `core/resources/ext/b18b.py` の G の選択が、`record=1` のときにだけ出す事象です。

## 10. `memory-connector`（記憶コネクタ）

### 役割

すべての部品が agent-memory への記憶の保存をここ経由で行います（`core/memory-connector/memconn/__init__.py`）。秘密・個人情報の検査（S-15.3）を通し、同じ内容は保存せずつながり合う形を確かめます。

### 口

| 口 | 場所 | 方法 |
|---|---|---|
| 中核の口 | `ax-core` の 8080 | 資格なし |
| 作業側の口 | `ax-work` の 8081 | プラグインのトークン。一覧が設定されていない間は全部 401 です |

### 経路（2 つの口で同じ）

| 方法 | パス | 用途 | スキーマ |
|---|---|---|---|
| POST | `/v1/remember` | 記録の保存。必須は `project`、`host`、`source`、`kind`、`content` です（`subloop` が無ければ `core`、`kind` は `decision` / `observation` / `issue` / `result` / `important_artifact` / `preference` の 6 種、`content` は 20000 文字まで）。`supersedes` があれば旧い記憶を記録として残して新しい物に置き換えます | `schemas/ext/b1/remember-request.schema.json` |
| POST | `/v1/search` | 検索。`query`、`limit`（1〜50）、`project`、`subloop`、`host`、`kind`、`include_history` を渡します | — |
| GET | `/v1/memories/{memory_id}` | 1 件の記憶 | — |
| GET | `/v1/memories` | 一覧（`limit`、`field`、`since`、`until`、`cursor`） | — |

保存の流れは次のとおりです（`memconn/service.py`）。

1. 必須項目と形の検査。外れれば 422 です。
2. 秘密・個人情報の検査。当たれば拒否する（置き換えて送らない）422 で、`safety.rejected` を書きます。
3. `memory_search` で同じ内容を探し、重複があれば保存せず、見つけた ID で `memory.distilled` を書きます（`duplicate: true`）。
4. `supersedes` があれば `memory_supersede`、無ければ `memory_remember` を呼びます。
5. 返ってきた ID で `memory.distilled` を書きます。

接続ができないときは `outbox/` に溜めて 202（`queued`）で返します。保存の範囲は共通記憶一つです（`visibility="project"`、`scope={"user":"<user>","project":"all","agent":null}`）。

### 出す事象

| 種類 | 意味 |
|---|---|
| `memory.distilled` | 保存した記憶の ID と記録の話題（`duplicate` か `unknown` か `reason` を含むこともあります） |
| `safety.rejected` | 秘密に当たる記録を拒否したとき |

## 11. `observer`（見張り）

### 役割

ホストの見張りと内部の監視です。`observe_s`（既定 60 秒）ごとに検査を全部回します（`core/observer/observer.py`）。

### 口

| 口 | 場所 | 方法 |
|---|---|---|
| 中核の口 | `ax-core` の 8080 | `/healthz` だけです |

書き込みの口を持ちません。設定が変わることはありません。

### 検査

`check_liveness`、`check_watch`、`check_expired_pending`、`check_lag`、`check_starve`、`check_repeat`、`check_manager_trouble`、`check_drain_silent` の 8 種類です（`DEFAULT_CHECKS`）。取ってくる事象は `error.classified`、`manager.cycle`、`manager.decision`、`inspection.phase` の 4 種類です。

### 出す事象

| 種類 | 意味 |
|---|---|
| `health.alert` | 検査の警報（`check`、`target`、`severity`、`destination`、`detail`） |
| `backup.activated` | 予備役の起動を呼ぶとき（予約の宛先 `backup` がある場合） |

警報の配送は `health.alert` を書き、必要に応じてホスト見張り（`host-requests/` に起動し直しの要求ファイルを出す）と、予備役のフックを呼びます。

## 12. `inspector`（点検係）

### 役割

定期の点検の予定と結果を持つ部品です（D-2.1）。

### 口

| 口 | 場所 | 方法 |
|---|---|---|
| 中核の口 | `ax-core` の 8080 | 読み取りだけ |

| 方法 | パス | 備考 |
|---|---|---|
| GET | `/v1/inspection` | 点検の状態（`phase`、`inspection_id`、`interval_h`、`next_at`、`quiet_streak`、`last_result` と、状態の数）と `timeline_ok` を返します |
| POST | `/v1/review-changes` | 501 です（`not_implemented`）。利用者による変更の記録は「利用者変更の受け渡し契約は未確定（D-12、OPEN.md）」であるとします |

### 出す事象

| 種類 | 意味 |
|---|---|
| `inspection.phase` | 点検の段階（`phase` と計画） |

状態は `state.json` に持ちます（`core/inspector/state.py`）。

## 13. `manager`（管理役）

### 役割

周回して判断する中核の部品です（B9。S-10、D-9）。

### 口

| 口 | 場所 | 方法 |
|---|---|---|
| 中核の口 | `ax-core` の 8080 | 読み取りだけ |

| 方法 | パス | 応答 |
|---|---|---|
| GET | `/v1/manager` | `{"cycles", "last_cycle_at", "last_tick_at", "timeline_ok", "inbox", "bad_streak", "decisions"}` |

書き込みの口はありません。背景のタスクで回し続ける仕組みで、時系列記録に届かなくても周回を続けます。

### 周回の契機

時系列記録の購読（`error.classified` の unknown、`health.alert` の宛先 manager、自分の `control.expired_pending`、`resource.repeat_failure`、`inspection.phase`、`job.state` の blocked）と、`manager_tick_min` ごとの時計です。

1 回の周回は命令・unknown・緩和・恒久修繕の ToDo・`check_at` を過ぎた `parked`・`manager.cycle` の順です。

### モデル

worker-pool の `POST /v1/jobs`（役割 `manager`、runner `chat-json`）だけを使います。モデルの入口にも鍵にも触れません。

### 出す事象

| 種類 | 意味 |
|---|---|
| `manager.cycle` | 周回の記録 |
| `manager.decision` | 判断の記録 |
| `error.classified` | モデルからの返答が使えない場合の分類（`class: artifact.bug` か `system.expected`） |

## 14. `archiver`（退避）

### 役割

1 時間ごとに巡回し、各ボリュームの `archive/` の中身を退避先へ転送します（`core/archiver/engine.py`）。

### 口

| 口 | 場所 | 方法 |
|---|---|---|
| 中核の口 | `ax-core` の 8080（固定アドレス `192.0.2.12`） | `/healthz` だけ |

圧縮ファイル・合成ファイルを作ったり、照合済みの manifest（`verified/`）を書いたりします。`reap_enabled` が使えるかが健康の見方に出ます。

### 出す事象

| 種類 | 意味 |
|---|---|
| `archive.moved` | 転送の成功（転送先、バイト数、移したファイルの数、消した物、残した物） |
| `archive.failed` | 転送の失敗（`source`、`reason`、`detail`、`count`。同じ原因は最初の 1 回と、24 巡ごとにだけ書きます） |

`archive.failed` はスキーマ `event-data` の定義に載っていません（自由な object として通用します）。`archive.moved` は `schemas/event-data.schema.json` の `$defs/archive.moved` で定義されています。

## 15. `egress-proxy`（外向きの中継）

### 役割

ジョブ用コンテナ（`ax-job`）の外向きの通信を許可先だけに絞る小さな asyncio の部品です（`core/egress-proxy/egress_proxy.py`）。

### 口

3 つです（すべて 0.0.0.0 で待ち受けますが、コンテナの網の中で待つだけです。compose の `ports` は持ちません）。

| 口 | 用途 | 行うこと |
|---|---|---|
| 3128 | HTTP プロキシ | `CONNECT` は、ユーザー名 `job-<id>`（`Proxy-Authorization` の Basic）に対応する許可ファイルのホスト名の 443 だけを通します。他は 403 です。素の HTTP の絶対 URI（CONNECT でない）のときは、許可ファイルにゲートの行 `@gate <port>` があるジョブのときだけ、ゲート（`EGRESS_GATE_HOST:<port>`）への中継だけを通します。宛先はこの 1 件だけです。許可ファイルが無い、読めない、ジョブ ID が不明のときは 3425 以外は全部拒否します |
| 3425 | モデルの入口 | `EGRESS_MODEL_GATEWAY` の URL のホストとポートへの TCP の中継です。許可は無条件 |
| 8080 | 健康の見方 | `GET /healthz` は `{"ok":true,"component":"egress-proxy","version":"0.1.0"}` を返します（`AX_HEALTH_PORT`、既定 8080）。他は 404 です |

許可ファイルは `${EGRESS_ALLOW_DIR}/job-<id>.allow`（1 行 1 ホスト名。launcher が書きます）。ログはホスト名とジョブ ID だけを出し、URL・ヘッダー・本文・資格は出しません。

`CONNECT` の宛先は名前解決の結果が公開アドレスのときだけ通します。プライベート、ループバック、リンクローカル、tailnet（carrier-grade NAT）のアドレスは拒否します。解決したアドレスへ直接つなぎ、名前で再接続しません（DNS の書き換えで内側へ向けられるのを防ぐため）。

環境変数は次のとおりです。

| 変数 | 既定 |
|---|---|
| `EGRESS_ALLOW_DIR` | `/egress` |
| `EGRESS_MODEL_GATEWAY` | （必須） |
| `EGRESS_GATE_HOST` | — |
| `EGRESS_GATE_PORT` | — |
| `EGRESS_LISTEN_HOST` | `0.0.0.0` |
| `EGRESS_PROXY_PORT` | `3128` |
| `EGRESS_GATEWAY_PORT` | `3425` |
| `AX_HEALTH_PORT` | `8080` |
| `EGRESS_CONNECT_TIMEOUT_S` | `10` |
| `EGRESS_IDLE_TIMEOUT_S` | `600` |
| `EGRESS_MAX_CONNS` | `256` |

この部品は時系列記録へは書き込みません。通信の記録は標準出力への JSON の 1 行です。

## 16. 事象

全部の事象は時系列記録（`timeline`）に集まります。事象の形は S-3.2（`schemas/event.schema.json`）です。`kind` ごとの `data` は `schemas/event-data.schema.json` の `$defs/<kind>` で定義されています。表にない `kind` は自由な object です。

次は `kind` が何で、どの部品が出すかの索引です。定義の有無は `schemas/event-data.schema.json` の `$defs/` の有無で確かめます。

| 種類 | 出すもの | 定義 |
|---|---|---|
| `health.beat` | 全部の部品（`axcommon/http.py` の `create_app`） | `$defs/health.beat` |
| `archive.moved` | `archiver` | `$defs/archive.moved` |
| `archive.failed` | `archiver` | （定義なし。自由な object） |
| `control.issued` / `control.applied` / `control.expired_pending` / `control.released` / `control.renewed` / `control.rejected` | `control` | `$defs/control.*` |
| `plugin.registered` | `control` | `$defs/plugin.registered` |
| `permission.checked` / `permission.changed` | `control` | （定義なし。自由な object） |
| `task.state` | `ledger` | `$defs/task.state` |
| `state.recorded` | `ledger` | `$defs/state.recorded` |
| `decision.recorded` | `ledger` | `$defs/decision.recorded` |
| `error.classified` | `ledger` と `manager` | `$defs/error.classified` |
| `events.rejected` | `timeline`（作業側の口で拒否したとき） | （定義なし。自由な object） |
| `health.alert` | `timeline`（秘密の索引の不具合）と `observer`（警報） | `$defs/health.alert` |
| `memory.distilled` | `memory-connector` | `$defs/memory.distilled` |
| `safety.rejected` | `memory-connector`（秘密に当たる記録を拒否したとき） | （定義なし。自由な object） |
| `health.alert`（索引の検査） | `timeline` | `$defs/health.alert` |
| `backup.activated` | `observer`（警報の配送） | `$defs/backup.activated` |
| `inspection.phase` | `inspector` | `$defs/inspection.phase` |
| `manager.cycle` / `manager.decision` | `manager` | `$defs/manager.*` |
| `job.state` | ジョブの状態の記録 | `$defs/job.state` |
| `env.selection` | `resources`（G の選択。`record=1` のとき） | （定義なし。自由な object） |
| `env.usage` / `sandbox.call` / `env.machine_clock` | `resources` の箱の実装（`box_mcp`） | `env.usage` だけが `$defs/env.usage` です |

## 17. JSON Schema の索引

全部のスキーマは `schemas/` の直下にあります。`$id` は `https://ax.invalid/schemas/<ファイル名>` です（`axcommon/schemas.py`）。内部の参照は相対で書き、検査器は `jsonschema`（Draft 2020-12）です。CLI は `python3 -m axcommon.schemas --check`（全部をメタスキーマで検査する）と `--validate NAME FILE` です。

`schemas/dest/` は入口の行き先（`intake-destinations`）の payload のスキーマで、別の文書で扱うものがあります（`api-intake-gate-hq.md` の「行き先の登録簿」）。

`schemas/ext/` は束（B0〜B18）の拡張スキーマです。`schemas/ext/b1/` がこの文書の時間と記憶の経路の本文で扱うものです（`remember-request` など）。束のスキーマは、検査が無いものも少なくありません。束ごとに使われるものだけ索引に載せます。

### この文書で使う主なスキーマ

| ファイル | 何の検査か |
|---|---|
| `api-error.schema.json` | エラー応答 |
| `common.schema.json` | 共通の型（`$defs` に `ulid`、`ts`、`ts_any`、`plugin_id`、`kebab_or_ulid`、`workdir_id`、`sensitivity`、`mode`、`plugin_kind`、`error_class`、`node_status`、`issued_by`、`via`、`tier`、`role`、`state_run`、`todo_state`、`projection_name`、`text_nosecret`） |
| `healthz.schema.json` | `GET /healthz` の応答 |
| `event.schema.json` | 時系列記録の事象 |
| `event-data.schema.json` | 事象の種類ごとの `data` |
| `event-post.schema.json` | `POST /v1/events` の本文（1 件か配列） |
| `ext/b1/events-post-response.schema.json` | `POST /v1/events` の応答（`accepted` / `duplicates` / `events`） |
| `ext/b1/events-list-response.schema.json` | `GET /v1/events` の応答（`events` と `next_after`） |
| `ext/b1/export-request.schema.json` | `POST /v1/export` の本文（`before_ts` / `to_seq` / `days`） |
| `ext/b1/export-response.schema.json` | `POST /v1/export` の応答（`exported` / `files` / `last_seq`） |
| `ext/b1/remember-request.schema.json` | `POST /v1/remember` の本文（`project` / `subloop` / `host` / `source` / `kind` / `content` / `supersedes` / `valid_from` / `refs`） |
| `ext/b1/remember-response.schema.json` | `POST /v1/remember` の応答 |
| `ext/b1/timeline-archive-index.schema.json` | 書き出しの索引 |
| `ext/b1/archive-manifest.schema.json` | 退避の manifest |
| `ext/b1/archive-sources.schema.json` | 退避元の一覧 |
| `ext/b3/decision.schema.json` | 判断の記録 |
| `ext/b3/goal.schema.json` / `promise.schema.json` / `schedule.schema.json` | 目標・約束・予定 |
| `todo.schema.json` | ToDo |
| `todo-transition.schema.json` | `POST /v1/todos/{id}/transition` の本文（`to` / `reason` / `check_at`） |
| `todo-answer.schema.json` | `POST /v1/todos/{id}/answer` の本文（`answer` / `answer_source` / `quote` / `version`） |
| `focus-state.schema.json` | `PUT /v1/focus-state` の本文（`state` / `since` / `as_of`） |
| `daily-state.schema.json` | `PUT /v1/daily-state/{date}` の本文（`date` / `tz` / `as_of` / `segments`） |
| `state-put-response.schema.json` | focus / daily の `PUT` の応答（`stored` / `kind` / `as_of` / `version`） |
| `control-command-request.schema.json` | `POST /v1/commands` の本文（`target` / `op` / `args` / `issued_by` / `expires_at` / `on_expiry` / `corr`） |
| `control-command.schema.json` | 制御命令（保存される形） |
| `permission-check.schema.json` | `POST /v1/permissions/check` の本文と応答 |
| `validate.schema.json` | `POST /v1/validate` の本文と応答 |
| `manifest.schema.json` | プラグインの manifest |
| `graph.schema.json` | `graph.yaml` |
| `node-io.schema.json` | ノードの入出力 |
| `subgraph.schema.json` | 部分グラフ |
| `work-item.schema.json` | 作業の項目 |
| `registry.schema.json` / `registry-fragment.schema.json` | 登録簿とその断片 |
| `resource.schema.json` | 資源台帳の資源 |
| `environment.schema.json` | 実行環境の台帳の項目 |
| `job-request.schema.json` | `POST /v1/jobs` の本文（`role` / `prompt` / `sensitivity` が必須。`tools` があれば `research` / `screen` / `assist` の役割で、`sensitivity` は `public` のときだけ） |
| `job.schema.json` | `GET /v1/jobs/{id}` の応答 |
| `plugin-status.schema.json` | `GET /status` の応答（`mode` / `knobs` / `running` / `queued` / `last_run`） |
| `plugin-apply.schema.json` | `POST /apply` の本文（`mode` / `knobs` / `revision`） |
| `projection-response.schema.json` | `GET /v1/projections/{name}` の応答 |
| `outbox-entry.schema.json` | outbox の 1 行 |
| `instruction.schema.json` | 入口の指示（`from` / `destination` / `payload` が必須） |
| `intake-destinations.schema.json` | 入口の行き先の登録簿 |
| `dest/*.json` | 入口の行き先の payload（別の文書で扱う） |
| `ext/b10b/api/hibi-now.schema.json` / `hibi-day.schema.json` | HQ の hibi-state の読み取り |
| `dest/emergency.json`（登録簿はこちらを指す。`ext/b10a/emergency.payload.schema.json` は同じ形の写し） | 緊急ボタンの payload |
| `ext/b10a/notify-action.schema.json` | 通知の処理 |
| `profile.schema.json` | `profile/<名前>.yaml` |
| `defaults.schema.json` | `config/defaults.yaml` |
| `error-rules.schema.json` | `config/error-rules.yaml` |
| `hibi-state.schema.json` | hibi-state の成果物 `state.json` |
| `runner.schema.json` | `config/runners/<名前>.yaml` |

## 18. 状態

この文書は現時点で未実装の部分も含めて書いています。コードで確かめていることと、まだ未実装のことは下に書きます。

### 確かめていること

- 全部の経路の追加と分類（`app.py` と `api.py` の `Route(...)` の読み合わせ）
- `Authorization: Bearer` の資格の判定（`workauth.py`、`callers.py`、`core_auth.py`）
- `healthz` の追加項目（`create_app` と `extra_health` の読み合わせ）
- `egress-proxy` の 3128 / 3425 / 8080 の扱い（`egress_proxy.py`）
- `POST /v1/timers/{id}/fire` の 501（`timer_transport_undefined`）と `POST /v1/review-changes` の 501（`not_implemented`）

### 未実装・未確定

- `POST /v1/envs/{id}/gate-token` と `/revoke` は 501 です（未実装の経路。資格の検査は通すが、発行の本体は `core/resources/envs/` が持つ）
- `GET /v1/child-evaluations` は `worker-pool` の文書にだけ載っていますが、実際の経路（`build_routes`）には載っていません
- `GET /v1/ledger` / `GET /v1/models/discovery` / `GET /v1/selections` は `resources` の口にだけあります。`worker-pool` の文書に書いてある「`resources` の API を呼ぶ」は、実際の経路にはありません
- `POST /v1/timers/{id}/fire` は 501 です（`S-7.8/F-18.4`：発火報告の到達経路と資格が未定義です）
- `worker-pool` の事象（`worker.selection` / `worker.usage`）は、形は `usage.py` の `event_data` で決まっていますが、今は呼び出さないため書きません
- `archive.failed`、 `permission.checked` / `permission.changed`、 `events.rejected`、 `safety.rejected` の事象は、`event-data.schema.json` の `$defs` に定義されていません（自由な object として通用します）

## 関連文書

- `api-intake-gate-hq.md`: 入口（`intake`）、関門（`gate`）、HQ の口
- `environments.md`: 実行環境の台帳の解説
- `security.md`: 入口と認証の脅威モデル

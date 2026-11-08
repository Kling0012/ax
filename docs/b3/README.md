# B3/B4 の束（台帳と制御 API）

台帳は b3-1、制御 API と共有設定は b3-2 が受け持つ。依存は b0 と b1。グラフ検査には b7 の実装を使う。

## 置き場所

| 物 | 置き場所 |
|---|---|
| 台帳 | `core/ledger/`。`python -m core.ledger` で動く |
| 制御 API | `core/control/`。`python -m core.control` で動く |
| Compose | `compose.d/b3.yaml`。`ledger`、`control`、状態ボリュームを初期化する `b3-volume-init` |
| 拡張スキーマ | `schemas/ext/b3/`。goal、promise、schedule、decision |
| 登録の断片 | `plugins/registry.d/b3.yaml`。中核はプラグインではないため、登録する物はない |
| 試験 | `tests/b3/`。共通の組立は `conftest.py`、偽の時計と通信は `tests/fakes/` |

## 口と資格

両方の部品は1つのプロセスで中核の口と作業側の口を開く。`core.timeline.netbind` を使い、8080 は ax-core と healthcheck 用の loopback、8081 は ax-work だけに bind する。全アドレスには開かない。外へのポートの公開はない。

| 口 | 通す物 |
|---|---|
| `ledger:8080` | S-9.3 と S-9.5 の API。書き込みは core-tokens の資格を検査する |
| `ledger:8081` | `POST/GET /v1/todos`、`GET /v1/todos/{id}`。作成者はプラグイン ID に固定する。集中状態・日次状態の `PUT/GET` は `hibi-state` だけに許す。ほかは404 |
| `control:8080` | S-5.7 の API とタイマーの API。書き込みは資格を検査する。`/v1/validate` と `/v1/permissions/check` は資格なしで使える |
| `control:8081` | `GET /v1/plugins`、`GET /v1/plugins/{id}`、`GET /v1/plugins/{id}/effective` だけ。プラグインの資格が必須。書き込みは404 |

作業側の資格は `AX_PLUGIN_TOKENS` のハッシュ一覧で照合する。既定のファイル名は F-18.4 の伏せ字に対応する `core.ledger.workauth.HASH_LIST_BASENAME`。Compose の bind 元は `AX_PLUGIN_TOKENS_FILE` で差し替えられる。この名前の確定は `build/OPEN.md` の既存項目に残る。

中核の資格は `AX_CORE_TOKENS` の一覧で照合する。既定は `/state/core-tokens.sha256`。制御命令の発行者 manager、backup、inspection、emergency は、それぞれの専用トークンで固定する。本文の `issued_by` が資格と違えば拒否する。入口を表す `intake` の資格だけが、入口で確かめた `user` または `commander` を中継できる。実際に入口へ渡す資格は F-18.4 の表に未定義のため、入口との接続は未完了である。

台帳の利用者操作では、入口が確かめた受け口を `X-AX-Via` で渡す。`user_only` のカードは台帳でも検査する。制御 API から対象の `/apply` と `/items` を呼ぶときは `control-apply-token` だけを使う。トークンがなければ送らない。

## 登録と命令

起動時に `plugins/registry.yaml` を読む。manifest の Schema、knob の既定値と範囲、項目を受けるサブループの `inbox_max` を検査する。サブループは b7 の `tools/ax_graph_check.py` の `errors_for_body(graph)` でも検査する。検査器がない場合や、検査が通らない場合は、そのプラグインだけを登録しない。

再登録しても revision は戻さず、利用者設定の印を保持する。命令、解除、再登録のたびに revision を増やす。利用者による全体の knob 命令も、各対象に印を付ける。

モードと knob は S-5.5 の優先順位で計算する。管理役・予備役・司令塔の全体命令、中核への命令、範囲外の値、許可されない発行者、期限の欠落や超過を拒否する。点検係は `on_expiry: release` が必須。`ask_issuer` の期限切れは `expired_pending` とし、効力を保つ。解除時は既定値または残る命令を `/apply` へ送り直す。反映済みの事象は、対象の `/status` の mode と knob が一致したときだけ書く。

解除は発行者のほか、利用者と緊急操作に許す。延長は解除済みの命令を復活させず、期限・理由・回数・保持期間の上限を検査する。期限の走査は10秒ごとに行う。

## タイマーと継続許可

管理役のタイマーは `at` と `every_s` の片方だけを受ける。寿命と件数を S-17 で制限する。重複 ID、過去の予定、不正な間隔、寿命内に起動しない予定を拒否する。`control.release` は管理役自身の命令だけを解除する。期限切れのタイマーは実行しない。

継続許可は利用者だけが作成・無効化できる。repo と destination を明示し、空の操作集合、未知の操作、重複、ワイルドカードを拒否する。`comment` と `label` は継続許可に載せない。条件がある許可は、条件を照合する API が未定義の間は許可しない。X・Discord・scheduled-publisher の送信処理は `collect_only` を既定とする。

## 配置

ledger/control は `${AX_UID}:${AX_GID}` で動く。`b3-volume-init` が専用の状態ボリューム、outbox、archive の所有者をそろえ、終了してから両サービスが起動する。初期化コンテナは網を持たない。

両サービスへは秘密そのもののディレクトリを渡さず、トークンのハッシュ一覧と秘密の索引を読み取り専用で bind する。個人情報の一覧 `pii-list` は secrets で渡す。台帳の秘密検査は `axcommon.redact.from_environment` を使い、検査器を作れない書き込みは503にする。制御 API の事象は outbox へ保存する前に同じ検査を使う。

主な環境変数は `AX_HOST`、`AX_STATE_DIR`、`AX_CORE_TOKENS`、`AX_PLUGIN_TOKENS`、`AX_WORK_PORT`、`AX_REDACT_INDEX`、`AX_PII_LIST`。制御 API はさらに `AX_REGISTRY`、`AX_GRAPH_CHECK`、`AX_TIMELINE_URL` を使う。既定の登録簿は `/ax/plugins/registry.yaml`。

## 未決の接続仕様

以下は `build/OPEN.md` にも記録する。推測で API、資格、Schema を足さない。

| 仕様 | 現在の動作 |
|---|---|
| S-5.3 と S-4.4 | `cancel`、`run_now`、資源の除外・復帰を対象へ配送する API がない。命令を保存せず501を返す |
| S-7.8 と F-18.4 | timer-runner は ax-work に属するが、タイマーの読み取りと発火報告の到達経路・資格が未定義。`GET /v1/timers` は中核だけに置き、HTTP の `/fire` は501を返す。実行体は変更しない |
| S-4.3 | モデル設定の複合値の登録キー・要素の型・範囲を宣言する Schema がない。該当する変更は501を返す |
| S-23.1、S-23.8 | 送信対象一覧を制御 API が照合する API がない。送信処理を `run` にする命令を拒否する |
| S-20.3 | 継続許可の条件を照合する要求・応答の形がない。条件付き許可の確認は `allowed: false` を返す |

## 検証

b3-2 で実行した確認は、`tests/b3/test_t5_control.py` の35件、担当 Python ファイルの `py_compile`、ダミー値を使う `docker compose config`。すべて通った。pytest は既存の uv キャッシュを `PYTHONPATH` へ足し、`-p no:cacheprovider` とネットワーク接続を拒否する共通 fixture を使った。同期試験のため pytest-asyncio を読み込まず、`asyncio_mode` の警告が1件出る。

T-4 と T-183 は b3-1 の担当であり、b3-2 では未実行。全体試験も実行していない。

| 実環境で行う確認 | この成果物づくりでの状態 |
|---|---|
| コンテナ起動、状態ボリュームの所有者、再起動後の revision 保持 | 未実行。サービスの起動は禁止されている |
| ax-work から中核の8080に届かず、作業側の8081だけに届くこと | 未実行。ソケットの待受と実ネットワークの試験は禁止されている |
| 本物の資格で `/apply` を送り、誤った資格を対象が拒否すること | 未実行。偽の通信で送信資格だけを確認した |
| timer-runner と制御 API の疎通、実行の報告 | 未実行。到達経路と資格が未定義であり、実行体の起動も禁止されている |
| 入口から利用者・司令塔の資格を中継すること | 未実行。入口へ渡す資格が未定義である |

# 通知配送の接続と確認

通知は HQ の中で保存し、配送先ごとの枠に従って送る。通知サーバーは profile の `ntfy_endpoint` と `ntfy_topic` で指定する。AX は既存の ntfy を起動、更新、停止しない。

## HQ への接続

`axcommon.ext.load_extensions` が `core/hq/ext/b10a.py` の `register(app)` を呼ぶ。`make_routes(hub, intake=..., control=...)` は偽の部品でも使える。

HQ 本体は `app.state.hq_auth(request)` を用意する。同期関数でも非同期関数でもよい。認証済みの利用者を表す辞書を返し、拒否するときは `None` を返す。認証が未接続、または確認できない場合は 503、許可外の利用者は 403 となる。本人ヘッダーを直接信用する処理はこの拡張に置かない。

通知の周期処理は HQ の lifespan に接続する。HQ の開始後に走り、終了時に止まる。既存の lifespan の返す状態を保持する。`AX_HQ_NOTIFY_RUN=0` なら周期処理を登録しない。試験ではこの設定か偽の Runner を使い、実際のスレッドを起動しない。

HQ 本体はこの作業時点では未実装で、認証と画面との結合確認は未実行だ。通知の `#/todo/<ID>` を受けた画面は同じ ID のカードを開く必要がある。

## API

| メソッド | 経路 | 内容 |
|---|---|---|
| GET | `/v1/notify/status` | 配送状態の件数、検査の可否、再通知時間、朝の要約時刻 |
| GET | `/v1/notify/open/{todo_id}` | 同じ ToDo、質問、選択肢、許可された操作 |
| POST | `/v1/notify/actions` | `todo_id`、`action`、`op_id`。保留には `reason` と `check_at` も必要 |
| POST | `/v1/notify/test` | テスト通知の受付確認。成功後の再送は 409 |
| GET | `/v1/emergency/scopes` | `all`、`commanders`、登録済みプラグインの ID |
| POST | `/v1/emergency` | `scope`、`confirm: true`、`op_id` を受け、入口の `emergency` へ渡す |

カードの完了と保留は入口の `ledger.complete` と `ledger.park` を使う。台帳を読み直して更新を確認してから成功を返す。不正な状態遷移は拒否する。同じ操作の再送には保存済みの結果を返す。質問への回答は HQ 本体から入口の `ledger.answer` へ渡す。同じカードの回答と `todo` への復帰は台帳が記録する。

緊急操作では HQ 自身がリセットや再起動を行わない。入口が認証、承認、冪等性を検査し、b10a-2 の実行体へ渡す。HQ は入口の応答を返し、受付と処理完了を同一視しない。

## 配置と設定

| 変数 | 用途 |
|---|---|
| `AX_HQ_DATA` | 通知の保存先。既定は `/run/hq/notify`。中に `notify.sqlite` を置く |
| `AX_NTFY_ENDPOINT` | profile の `ntfy_endpoint`。実在するホストの値は profile で渡す |
| `AX_NTFY_TOPIC` | profile の `ntfy_topic`。既定は `hub` |
| `AX_HQ_BASE_URL` | 通知から HQ を開く基点。未設定なら click を付けない |
| `AX_LEDGER_URL` | 台帳の読み取り先。既定は `http://ledger:8080` |
| `AX_HQ_INTAKE_SOCKET` | HQ 口のソケット。既定は `/run/hq-intake/intake.sock` |
| `AX_REDACT_INDEX` | 秘密の索引のファイルの場所 |
| `AX_PII_LIST` | 個人情報の登録一覧のファイルの場所 |
| `AX_HQ_NOTIFY_INTERVAL_S` | 周期の秒数。既定は60秒 |

通知の保存先は HQ のソケットのディレクトリの bind の下に置く。HQ に4つ目のホスト bind を足さない。compose の変更は b10a-2 が担当する。秘密の値、許可利用者の一覧、実在するホストの値を git に置かない。

`NotifyHub.apply_knobs` は `question_renotify_h` と `morning_digest_time_jst` を受ける。制御 API での knob の所属と値の取り込み経路は未接続だ。ntfy の枠は `ntfy_rate_per_min` の既定値から読む。Discord は毎分10件を実装上の既定とし、送信体は注入された場合だけ使う。自動送信の開始許可の確認は送信元が担当する。

## 配送状態

枠を超えた通知は `held` で保持し、枠が空いた後に再配送する。接続失敗は `pending` で保持し、待ち時間を置く。検査で拒否したものは `blocked`、再試行の上限に達したものは `failed` となる。他の配送先の処理は続く。旧メールの3件制限と旧単発通知は引き継がず、旧 hub-watch も起動しない。

本文と件名は省略する前に検査する。ntfy では秘密と個人情報を伏せ、Discord は送信を拒否する。参照、topic、tags に秘密や個人情報があれば送らない。検査用ファイルを読めない場合も拒否する。検査を通らない本文は保存しない。索引と登録一覧は配送時にも読み直す。外部中継へのリクエストは作らない。

質問の通知は ToDo ID と質問のバージョンで対応付ける。同じ未回答の質問は成功した通知から既定の48時間が過ぎるまで再通知しない。回答済みや閉じたカードの保持分は、新しい通知を配送する前に取り消す。

朝の要約は日本時間の前日を対象にする。取得失敗は「取得できなかった」と表示し、0件と扱わない。件数と取得失敗の表示を明細より先に置く。同じ対象日の通知を作り直さず、保存済みの配送状態を確認する。HTTP 応答が失われた場合の実際の配信結果は、この試験では確認していない。

## 手元の試験

リポジトリのルートから、既存の依存がそろった環境で次を実行する。試験はソケット接続を禁止し、偽の HTTP 転送を使う。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 TMPDIR=<temp-dir> \
  python3 -m pytest -p no:cacheprovider tests/b10a/notify/
```

この環境では pytest などを追加導入せず、既存の uv キャッシュを `PYTHONPATH` に足した。Python 3.12 用の rpds も指定した。pytest-asyncio を読み込まないため、`asyncio_mode` の設定について警告が1件出る。

## B17 の実環境の確認

次の試験は未実行だ。この成果物を作る間は実機とサービスに触れないため、切り替え担当が確認する。

| 対象 | 確認すること | 今回の状態 |
|---|---|---|
| T-55 受付 | 実際の ax-core から非公開 ntfy へテスト通知を1件送り、HTTP 200 と ID を確認する | 未実行 |
| T-55 到達 | 同じ ToDo カードに端末への到達の回答を残す。回答を待たず、カードを開いたままにする | 未実行 |
| T-55 中継 | 外部中継へ本文や個人情報が渡らず、topic とメッセージ ID だけとなる設定を確認する | 未実行 |
| T-53 / T-54 | 実際の HQ の認証、画面、入口、台帳を通して回答、完了、保留を確認する | 未実行 |
| 緊急操作 | 実際の HQ から入口を経由し、b10a-2 のスナップショットと範囲指定を確認する | 未実行 |
| 永続化 | HQ のソケットの権限を保ったまま通知の保存先へ書け、HQ 再起動後も保持分が残ることを確認する | 未実行 |
| T-81 | 旧送り手の停止後に AX だけが送り、旧単発通知と旧 hub-watch が動かないことを確認する | 未実行 |

HQ 本体との認証、`#/todo/<ID>` の画面、`AX_HQ_BASE_URL` の供給、制御 API の knob の接続、完了と保留の入口への登録は、後続の統合で確認する。テスト通知の到達確認用カードを作る処理も切り替え担当と接続する。実機の通知をこの手順書の作成中に送らない。

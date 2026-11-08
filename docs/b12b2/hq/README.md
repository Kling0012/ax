# HQ の承認と司令塔の接続

HQ は保存済みの承認内容を表示する。第二要素を検証し、intake の `hq` 口へ承認と席の操作を送る。差し込み口は `core/hq/ext/b12b2.py` の `register(app)` である。サービスの起動や待ち受けは行わない。

## 接続する部品

統合時に `app.state` へ次の関数と設定を渡す。

| 名前 | 渡す物 |
|---|---|
| `hq_approval_identity` | 同期か非同期の `request -> security.Identity`。S-22.7 の身元と接続元を検証する。`subject` と `session` は不透明な識別子とし、ログイン名は使わない |
| `hq_approval_browser_policy` | `BrowserPolicy(hosts, origins)`。値はプロファイルから渡す |
| `hq_approval_proof_encoder` | `(method, path, body, VerifiedAction) -> body`。intake の `hq_verifier` と組で接続する。種とコードは渡さない |
| `hq_factor_emit` | 私的 outbox の事象を時系列へ送る関数。`id` で重複を防ぐ。`at` を時刻に使い、登録の事象の data には時刻だけを入れる |
| `hq_factor_notify` | 同じ outbox の通知を送る関数。`id` で重複を防ぐ。コードと種を送らない |
| `hq_seat_registration_display` | intake の登録応答を `RegistrationDisplay` へ写す関数。コードと公開鍵の指紋だけを画面へ渡す |
| `hq_seats_presenter` | 席の照会応答を画面用に写す関数。直近の拒否とロックの応答形式が決まった後に接続する |

試験では `hq_approval_intake` と `hq_approval_factor` に偽の部品を渡せる。本番の intake の既定経路は既存の Unix transport と `/run/hq-intake/intake.sock` を使う。HQ は席の保存先や gate を直接操作しない。

認証、Host/Origin、検証結果の wire が未接続なら 501 を返す。推測したフィールドは送らない。`seat.add` は既存 Schema の未決をそのまま受け、501 で止める。SSH の登録画面も無効な操作として表示する。

## 画面と操作

| 経路 | 用途 |
|---|---|
| `GET /seats` | 席、承認待ち、第二要素、ホスト操作の警告を表示する |
| `GET /approvals/{approval_id}` | intake の保存済み内容を表示する |
| `GET /approval/settings` | 第二要素の登録状態を表示する |
| `POST /v1/hq/approvals/{approval_id}/grant` | 画面に結び付いた digest を承認する |
| `POST /v1/hq/approvals/{approval_id}/deny` | 承認待ちを却下する |
| `POST /v1/hq/seats/prepare` | Schema を検査し、席の操作内容を確認画面に保存する |
| `POST /v1/hq/seats/apply` | 確認画面の内容を intake へ送る |
| `POST /v1/hq/approval/enroll` | TOTP の QR を一度だけ返す |

承認、却下、登録、段の変更、解除では第二要素を求める。席の失効とロックでは求めない。POST は画面と身元とセッションに結び付いた CSRF トークンを一度だけ使う。トークンが失効した場合や入力を間違えた場合は、画面を開き直す。

承認の POST はクライアントから digest や payload を受けない。画面を作ったときの digest を送る。席の操作も確認画面の保存済み内容を送る。intake が承認待ちを返した場合は、保存された行き先と payload が確認画面と同じ場合だけ承認する。

`go.confirm` の全文は HTML として解釈せず、エスケープした `pre` に出す。`ratify` の範囲と変更の番号は intake の `derived` から読む。司令塔の説明は固定欄から分け、未検証の枠に出す。登録コードと QR の応答には `no-store` を付ける。再表示用の API はない。

## 第二要素と権限

種と最後のステップ、失敗回数、ロック期限は `/run/hq/private/approval-totp` の JSON に保存する。ホストでは `${ax_state_dir}/hq/private/approval-totp` となる。親の `hq/` と `private/` は 0700、ファイルは 0600 とし、実行 uid が所有する。権限が違う場合は修復せず拒否する。

検証関数は `factor.verify_code` の一つにまとめた。RFC 6238 の SHA-1、6 桁、30 秒、前後 1 ステップを使う。同じステップの再使用を拒否する。既定は 5 回の失敗で 15 分のロックである。値は共通の defaults から読む。`auth.failed` は `audit_window_s` でまとめ、ロックへの移行は別の理由で記録して通知する。

QR は追加依存のない byte mode、version 8、誤り訂正 L、mask 0 で作る。外部の QR サービスは使わない。初回登録は本人の直接経路と CSRF を要し、登録済みの取り替えは現在のコードも要する。

復旧用の HTTP 経路はない。ホストの復旧と席の直接登録の事象は、信頼する時系列アダプタから `FactorService.observe_host_event` に渡す。`approval.factor.reset` と `seat.host_added` を 24 時間表示する。ホストの処理とその通知は他担当の所有であり、HQ は実行しない。

守る相手は遠隔の席、コンテナ、operator 以外のユーザー、エージェントの文章である。operator の uid で動き sudo を使える物は守る相手に含めない。実 uid、ソケットの権限、ホストでの復旧と通知は実環境で確認する。

## 確認の範囲

専用試験は `tests/b12b2/hq/` にある。ASGI を直接呼び、ネットワークとサービスは使わない。T-189、T-190、T-193、T-194(f)(i)、T-211 の担当部分を検査する。実環境に残る確認は同じフォルダの `realenv.json` に列挙した。

実画面の描画、Alt+N の操作、認証アプリでの QR の読取は未実行である。この作業ではサービスの起動とブラウザの使用が禁止されている。画面の見た目と QR の光学読取を、試験の合格には含めない。

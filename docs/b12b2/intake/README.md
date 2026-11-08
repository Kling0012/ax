# 承認部品の接続手順

`core/intake/approval/` は S-6.9〜S-6.12、S-6.20、S-6.22、S-24.4、S-24.5 の部品を持つ。自動登録用のファイルはこの担当に含まれない。統合側が `integration.register()` を明示的に呼ぶ。

## 登録

1. intake の既存の SQLite ファイルを `ApprovalStore(path)` に渡す。別の本番保存先は推測しない。表の列は D-5.1 の `approvals` と同じである。
2. `AuthorityPolicy` に、席の状態を読む関数、検証済みの manifest を読む関数、台帳の `get_todo`、継続許可を照合する関数を渡す。席の状態を読む関数は拒否する場合に `seat_revoked`、`seat_expired`、`locked`、`lockdown` などの理由を返す。返す値は本文から作らない。
3. `ApprovalAudit` に既存の `EventSink` と `AccessLog` を渡す。`caller_reader(ident)` は接続から確かめた `device`、`key_fp`、`session` と、自己申告の `declared.agent`、`declared.model` を返す。指示本文の `session` を事象の身元には使わない。
4. `ApprovalService` に policy、audit、go、executor、card_writer、notifier を渡す。時間の設定は S-17 の `approval_ttl_min`、`go_md_max_bytes`、`audit_window_s` から渡す。
5. `integration.register(app, service, hq_verifier=...)` を呼ぶ。自動登録を行う `core/intake/ext/b12b2.py` と、指定の `core/intake/go_confirm.py` は統合側で配置を解決する。

`register()` は既存の入口アプリに次の経路を足す。新しい待受は作らない。

| 経路 | 処理 |
|---|---|
| `GET /v1/approvals` | HQ に保存した一覧を返す |
| `GET /v1/approvals/{id}` | HQ に保存した内容を返す |
| `POST /v1/approvals/{id}/grant` | 保存した digest を照合して実行する |
| `POST /v1/instructions` | 段・受け口・本文・user_only を検査し、必要なら承認待ちを保存する |

## HQ の確認結果

`hq_verifier(scope, body)` は `VerifiedUser(tailnet_allowed, second_factor, direct_tailnet)` を返す。これは内部の接続用の型で、HTTP の本文の項目ではない。HQ の検証結果をどの通信項目で渡すかは S-6.10 に名前がないため、この関数の既定の実装は用意しない。未接続なら承認用の経路は 501 になる。

HQ の uid は既存の受け口が `hq_ident` で確かめ、`scope.ax_ident` に置く。確認結果を本文の自己申告から作ってはならない。第二要素の種、コード、再使用の検査、失敗回数の上限、ロックの期限は HQ が持つ。intake はそれらを受け取らない。公開入口での承認は 404 にする。

## 保存と実行

承認の digest は正規化した payload の SHA-256 である。キーを並べ、空白を除き、UTF-8 にする。go の全文も payload に一度だけ保存する。導いた値、依頼 ID、接続の表示用の値、承認者の固定値 `user` は `result_json` に持つ。

`executor(command)` は保存した payload、project、session、expires_at と、`issued_by`、`approved_via`、`confirm_irreversible` を受ける。統合側は承認済みの書き込み専用の資格を持つ `axcommon.client` に接続する。司令塔の転送用の資格は使わない。行き先ごとの本文の変換と資格が未接続なら、実行前に `NotImplementedError` で止める。

承認の消費を SQLite に確定してから executor を一度だけ呼ぶ。実行中の応答が不明なら `state=granted`、`result_json.execution=uncertain` を残す。自動で再実行しない。保存先を開き直しても同じ承認は再使用できない。

`card_writer(card)` は承認 ID を ToDo の ID として使い、`waiting_user`、`kind=approval`、`user_only=true` のカードを作る。失敗時は `card_pending` を残し、統合側が `deliver_card(id)` で再試行する。台帳への作成は同じ ID で重複しない接続にする。notifier は承認待ちの通知と HQ へのリンクだけを送る。承認ボタンを作らない。

## go と照会

`GoConfirm` は `validate`、`parse_go_md`、`execute`、`verify_view`、`export_view` に対応する。`parse_go_md` はモジュールの関数である。行き先の Schema は b12b2-2 の `schemas/dest/go.confirm.json` を読む。

`export_view(id)` は UTF-8 のバイト列を返す。書き出す CLI 側が 0600 と既存ファイルの上書き禁止を守る。`verify_view(id, go_file_bytes, build_go_bytes)` は S-24.5 の data を返す。`checks` に `failed` か `mismatch` があれば、CLI 側は終了コード 1 にする。追認だけでは撤去の record 検査は通らない。

照会の応答は `external_view` で notice と untrusted_fields を付ける。人が読む外部の文章は `human_external` で各行に `[外部]` を付ける。CLI と MCP の実装側も同じ文を使う。`commander.read.summary` の集計は b12b3、SSH の接続の開始と終了は b12b1 に接続する。この部品は集計を重ねて実装しない。

## 試験の範囲

`tests/b12b2/intake/` だけを pytest で実行する。共通の conftest がネットワーク接続を拒否する。偽の HQ、第二要素、executor、席を使い、実際の B5 アプリと B3 台帳、および b12b2-2 の go Schema との接続を確かめる。

| 試験 | 実機で確かめる内容 |
|---|---|
| T-189、T-190 | 本物の HQ と第二要素、公開入口の 404、実際の書き込み資格 |
| T-192 | SO_PEERCRED、SSH の接続事象、実際の時系列停止と復旧、b12b3 の集計 |
| T-193 | 実際の CLI 表示、MCP initialize の notice、HQ のエスケープ |
| T-194 | CLI のファイルと標準入力、0600、既存ファイルを上書きしないこと |

これらの実機試験は B17 で行う。この成果物の作成では本番の設定やサービスを操作しない。

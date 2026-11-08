# 統合の接続

統合の正本は `integration.ports` の Protocol と `Ports` である。
`contracts.py` は同じ型を再公開する。別の型は定義しない。
`gate.py` が純粋なマージ判定、`flow.py` が外部操作と経路、`journal.py` が保存を担う。

## 収集

`CollectionAdapter` は b14-1 の `CollectionService` と `CollectionStore` を受け取る。
`collect(dry_run=..., mode=...)` は巡回を作り、外部状態を照合してから収集する。
実行するモードでは `claim_next` で排他を取る。
収集だけのモードと dry-run では排他を取って作業を起動しない。

`item_builder(row, key, target)` は、認証済みのプロバイダから作者、実装 PR、案件の種別を補う。
返す項目には次を持たせる。

| 項目 | 内容 |
| --- | --- |
| `provider`, `issue_repo`, `kind`, `number` | 収集したキーと同じ値 |
| `implementation_repo` | 登録された実装先 |
| `pr` | 実装先の PR 番号。未作成の間は `null` とし、Issue の番号とは区別する |
| `author` | プロバイダで確認した作者 |
| `project` | 対象のプロジェクト |
| `route` | `question`、`bug`、`pr`、`change` のいずれか |

作業の開始時と外部操作の直前に排他を確認する。
開始した項目の外部 ID を `record_submission` に記録する。
期限切れの排他を使う操作は停止する。
完了時は `finish`、保留時は `hold` へつなぐ。
利用者待ちや内部判断待ちの再開は、カードや判断の結果を確認した接続側が行う。

コメントは `CollectionService.comments` で全件を照合する。
切り替え前のコメントは文脈だけに使う。
新しいコメントは b14-2 の安全審査を通し、要求へ反映してから明示的に確認済みとする。
未確認のコメントを、取得できなかったことを理由に空の一覧へ変えてはならない。
閉じた Issue の再開も安全審査済みの判断を要する。

## 安全審査と作業

安全審査には b14-2 の `SafetyAdapter` を再利用する。
結果は `status`、`head_sha`、`input_revision`、二者の `reviews`、`worker_input` を持つ。
`reviewer` が異なる二者であること、両者が `safe` であることを統合でも確認する。
作業者には `worker_input` の検査済み要約だけを渡す。
生の本文、差分、コメントは作業入力へ混ぜない。

`WorkPort.plan` は対象の設定から TDD の `base_ref`、`branch`、`verify` を返す。
案件本文に書かれたコマンドをそのまま使ってはならない。
`WorkPort.run` の `tdd-change` は共通の部分グラフを実行する。
`answer` と `prepare_publication` は文章を準備し、外部への投稿は行わない。
作業側は渡された `model_budget` を守り、使った呼び出し数を `model_calls` に返す。

## 検証

`VerificationAdapter` は b14-3 の `run_verification` を呼ぶ。
実行先は渡された runner だけである。
`request_builder` が変更ファイルと実行環境の情報を補う。
repo、base、head、信頼区分、recipe、禁止パス、通信の許可先は、統合が確定した値で上書きする。
検証不能なら `parked` とし、成功へ置き換えない。

証拠は S-19.5 の項目を持つ。
統合のマージ判定も b14-3 の `complete` と `compare` を使う。
未信頼 PR では同じ head の二つの attempt と base を照合する。
完了していない実行、timeout、切断した出力、消えたテスト名を免除しない。

## 直前の GitHub 状態

`GitHubPort.view(item, target)` は新しい状態を読み、次を返す。
形は `schemas/ext/b14/integration/snapshot.schema.json` に定義する。
一覧は最後のページまで読む。取得不能や照合不能を成功の値へ変えない。

| 項目 | 確認する値 |
| --- | --- |
| `repo`, `pr`, `head_sha`, `base_sha` | 実装 PR と完全な SHA |
| `comments_complete`, `unprocessed_comments` | 取得の完了と未処理の一覧 |
| `input_revision`, `requirements_revision` | 本文とコメントの版に結び付けた要求の版 |
| `required_ids`, `requirements` | 全要求と完了の証拠 |
| `mergeable`, `state`, `draft`, `can_merge` | 競合、状態、draft、GitHub の権限 |
| `ci_complete`, `ci` | 設定された出所ごとの最新 CI。`origin: configured` だけを採用する |
| `publication_head`, `outbound_checked` | 全差分、本文、成果物の外向け検査と PR 更新を確認した head |
| `collect_only_reviewed_head` | 未信頼 PR の収集だけの確認を済ませた head |

これらの確認値を PR 本文から読み取ってはならない。
PR が未作成でも候補 head の検証証拠と要求一覧を同じ形で返す。
この段階では `pr` を `null` とできる。`mergeable` は PR がある段階だけ判定に使う。
PR 作成の配送結果は登録された repo と正の PR 番号を返さなければならない。
一致しない結果は採番せず保留する。
`merge(repo, pr, sha=..., method=...)` は同じ SHA を期待値として渡す。
戻り値は `merged: true`、`head_sha`、完全な `merge_commit_sha` を持つ。
応答が失われた場合は `reconcile_merge` で照合する。照合不能なら `lost` とし、自動で再送しない。

## 権限と配送

権限の確認は制御 API の `POST /v1/permissions/check` と同じ scope を使う。
`project`、`repo`、`destination`、`operation` を操作の直前に確かめる。
未信頼 PR の個別指示は `instruction` が認証済みのカード回答から返す。
`repo`、`pr`、`head_sha`、`operation: merge`、`source: user`、`card_id`、`instruction_ref`、`active: true` を要する。
エージェントの文章や項目のフィールドは個別指示として採用しない。

通知、回答・PR 更新、公開待ちは `DeliveryPort` で接続する。
`send(id, destination, body)` と `reconcile(id, destination)` は、同じ ID の配送を照合する。
成功は `status: delivered` と配送を確認した epoch 秒の `delivered_at` を持つ。
`absent` は未配送を確認できた場合だけ返す。
不明なら `unknown` とし、確認できるまで再送しない。
送信上限に達した場合は `retry` と `next_retry` を返す。
各呼び出しの期限は120秒未満とし、接続側でも同じ ID の重複を防ぐ。

公開待ちの一意キーは repo と PR である。
引き渡す記録は repo、PR、検証した head、マージ結果、元の Issue・PR のキーを含む。
公開や Issue の完了は公開側が判断する。
通知、投稿、成果物を含む全配送の本文を `outbound_check` で検査する。
検査できない場合は送信しない。

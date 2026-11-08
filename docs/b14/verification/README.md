# 変更の検証

S-19.5 の検証は `plugins.subloop.change.verification.run_verification` を呼ぶ。
実行物は b6b の launcher と b14b1 の環境入口を使う。この部品自身はプロセスを起動しない。

```python
run_verification(request, runner, scrub=scrub, cache=cache, recipes=None)
```

`request` は `schemas/ext/b14/verification/request.schema.json` に従う。
GitHub の全変更一覧から `changed_files` を作り、取得完了を `files_complete` に入れる。
各ファイルは `path`、`status`、`binary` を持つ。rename は `old_path` も持つ。
SHA は省略せずに渡す。未知の作者を信頼済みに変換しない。

`targets.yaml` の `test_recipe` は次の参照を使う。対象登録は b14-1 が行う。

| 対象 | 参照 | verify_runner |
| --- | --- | --- |
| Dreamforge | `verification/recipes.yaml#dreamforge` | `verify-dotnet` |
| hibi | `verification/recipes.yaml#hibi` | `verify-node` |
| hibi-server | `verification/recipes.yaml#hibi-server` | `env:hub-host-local:hibi-server-e2e` |
| hibi-memory | `verification/recipes.yaml#hibi-memory` | `verify-python` |

レシピは JSON としても読める YAML である。repo の所有者名は登録側で決める。
依存取得には対象設定の `outbound_allowlist` が要る。未設定では実行を保留する。
依存取得と検証は別のジョブで行い、検証には空の許可先一覧を渡す。
Dreamforge のビルドでは依存を再取得しない。ゲーム資産は `sod-gamedata` だけを読む。
hibi の unit・e2e は `unperformed` に残す。

## 実行口の接続

runner は同期の `prepare(invocation)` と `execute(invocation, prepared)` を持つ。
`prepare` は信頼された実行側で測定し、PR が出力した自己申告を使わない。
同じ `workdir_id` の依存を保持し、各 `attempt` は別の worktree にする。
各呼び出しで対象 SHA を照合する。実行後の `sha` も照合する。

隔離の測定値は `job_id`、`sha`、`environment`、`available` に加え、次を持つ。

- `isolation_verified`、`home_absent`、`credentials_absent`、`host_config_absent`、`network_enforced` はすべて真。
- `network_mode` は依存取得で `allowlist`、検証で `none`。
- `allow_hosts` はその呼び出しの現在の一覧と一致する。

`B6bRunner` には launcher の `job_start`・`job_status`、測定器 `attest`、回収器 `collect` を渡す。
回収器は期限を守り、ジョブが終了するまで待つ。切断や時間切れは成功に変換しない。
`/work/repo` のチェックアウトと作業ディレクトリの用意は測定器側で行う。
検証イメージにこの Python パッケージを導入する必要はない。

hibi の privacy 検査では、信頼された測定器が設定場所
`~/.config/hibi/privacy-patterns` を読み、ジョブ専用の `/work/privacy-patterns` に用意する。
`privacy_patterns_staged` が真のときだけ検査する。設定ファイルやパターンが無ければ保留する。
検査は固定の Python コマンドに展開する。パターンや一致した本文は出力しない。
`git grep` のエラーも失敗にする。ホストの設定ディレクトリ全体は渡さない。

`EnvironmentRunner` の `submit`・`status` は b14b1 を通して環境 `hub-host-local` に結び付ける。
汎用の環境選択へ丸投げせず、登録済みの `hibi-server-e2e` だけを呼ぶ。
測定結果には `entry_verified: true`、`env_id: hub-host-local`、対象の `worktree` が要る。
呼び出し側からシェルコマンドを渡さない。入口の並列上限1と2400秒の期限は環境側で守る。
`hibi-server-e2e` を実行できなければ、そのジョブだけを `parked`、理由 `verify_unavailable` にする。

## 証拠と判定

`execute` の戻り値には `job_id`、`sha`、`executed`、開始・終了時刻、終了コード、
`timed_out`、`complete`、`output_truncated`、`output_tail`、`test_names`、`failure_evidence` を入れる。
`test_names: null` は一覧を取得できない検証を表す。空の配列は取得済みの0件である。
`failure_evidence` は失敗したテスト名から診断本文への対応表である。
名前や診断を取得できない失敗には、base 由来の免除を適用しない。
出力末尾だけを切り出した場合と、出力の回収が途中で切れた場合を区別する。
後者は `output_truncated: true` にする。

戻り値は `status`、`reason`、`evidence`、`comparisons`、`unperformed` を持つ。
`status` は `passed`、`failed`、`parked` のいずれかである。
`passed` は検証の合格だけを表す。マージの許可は b14-4 が別に確認する。
証拠は S-19.5 の全項目を持つ。base の証拠の `head_sha` には実際に実行した base SHA が入り、
`revision_role: base` で区別する。head の2回は `head-1` と `head-2` を使う。
同じ実行 ID を2回に数えない。

`scrub(obj) -> obj` は必須である。共通の Redactor なら
`lambda obj: redactor.redact_obj(obj)[0]` を渡す。
出力だけでなく、テスト名と診断も秘密・個人情報の検査を通す。
比較用の診断が伏せられた失敗を、同じ失敗だとみなさない。

`EvidenceCache` は呼び出し側で保持する。完全な結果だけを保存する。
head・設定版・コマンド・種類・attempt に加え、repo・base・環境・現在のレシピと通信方針も照合する。
新しい head、競合解消、設定変更では照合をやり直す。
検証失敗は `comparisons` の理由と実行 ID から証拠をたどる。

## 受け入れ時の確認

オフラインの試験は `tests/b14/verification` だけを実行する。
移行時には実行側で次を確認する。

| 確認 | 条件 |
| --- | --- |
| b6b の隔離と通信 | 実際のマウント、HOME、資格、ホスト設定、許可先の実測 |
| チェックアウト と回収 | 完全な SHA、独立した worktree、出力の完了、テスト名と失敗診断 |
| hibi privacy | 設定ファイルの存在、ジョブ専用への受け渡し、秘密の非出力 |
| hibi-server | 入口の検証済み登録、排他、期限、終了時の後片付け |
| 4対象の実レシピ | 必要な依存取得先、イメージ、資産、コマンドの成功 |

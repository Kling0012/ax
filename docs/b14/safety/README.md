# b14-2 安全審査の接続手順

S-19.3、S-19.4、S-19.7 の入力処理と要求照合を提供する。公開口は `plugins.subloop.change.safety` にある。ネットワーク接続や外部操作は開始しない。GitHub、モデル、台帳、カードへの接続は b14-4 のアダプタに渡す。

## 接続の順序

1. 項目の識別には `{provider, issue_repo, kind, number}` を使う。実装先 repo と区別する。`AdmissionGate.reserve` で受付数を確保してから前処理へ進める。
2. `prepare_input(body, diff)` を呼ぶ。本文と差分は各々 UTF-8 の元のバイト数を検査する。コメント本文も同じ関数を使う。超過分は保留し、切り詰めない。戻り値の `removed` に除去の有無が入る。
3. 未信頼項目は `preclassify` に渡す。`close_candidate` は閉鎖の候補である。`close_plan` の権限確認と外向け検査も必要になる。異なる結論、不明、欠落では二者審査へ進む。
4. `SafetyGate.review` に前処理済み入力を渡す。作業者へ渡せる値は`state == ready` の場合の `worker_input` だけである。`reviews` は審査の記録であり、作業者への入力に混ぜない。
5. `RequirementBook.ingest(source=..., review=..., extract=...)` で要求を抽出する。抽出関数には審査済みの二つの要約だけを渡す。復元時は保存済みの要求一覧をコンストラクタに渡す。
6. 工程の区切りとマージ直前に `process_comments` を呼ぶ。新しい人間のコメントを見つけた巡回ではマージを許可しない。安全なコメントから要求を更新し、次の確認で未処理がないことを確かめる。
7. `RequirementBook.reconcile` に全要求の ID と証拠の照合関数を渡す。未完了、欠落、照合不能、新規コメントがあれば修正へ戻す。証拠の照合関数は対象 head と検証結果の一致も確認する。

## モデルの接続

`Candidate(id, resource, call)` の `id` は登録簿のモデル ID と一致させる。二つの審査枠には別のモデル ID を渡す。各枠のリストには S-8 の適格な予備候補を並べる。機密区分は `axcommon.policy.allowed` で送信前に照合する。

`call(request)` は既存のモデル実行口に接続する同期アダプタである。要求は `{purpose, input, tools: [], sensitivity, independent: true}` を持つ。毎回独立した会話で実行し、ツールを与えず、他方の判定や会話を渡さない。期限と巡回のモデル予算は S-8 と収集側の予算口で制限する。部分的な schema の結果には適格な予備候補を使う。時間切れや実行障害はその項目の停止として返す。

判定は `{verdict: safe|unsafe|unknown, concerns: [malicious|security|quality], summary}` である。前処理の事前分類には `{classification: relevant|spam|unrelated|unknown}` を使う。これは内部の判定形式であり、新しいサービス API ではない。

`injection_check({summary})` は既存の検査口に接続し、検査できて安全な場合だけ真を返す。各要約に対して機械検査とこの検査を行う。機械検査は URL、コマンド、指示文、隠し文字の残存を拒否する。意味の判定は接続先の検査も必要とする。要約全体にも `change_input_max_bytes` を適用する。

`SafetyAdapter(gate, sensitivity_of=...)` は b14-4 の `SafetyPort.review(item)` に接続できる。`item.snapshot` に `body`、`diff`、`head_sha`、`input_revision` を渡す。差分がない Issue では `diff` を空文字にする。成功時は `status: passed`、対象 head、入力版、`reviewer` を付けた二つの判定を返す。`sensitivity_of` は信頼できる対象設定から機密区分を読む。本文から区分を決めない。

## コメントと確認位置

入力レコードは `{repo, item_number, comment_id, author, created_at, revision, origin, body}` である。`origin` は収集側が記録した `human`、`automation`、`ax` 等の値を渡す。アカウント名だけでは人間と判断しない。AX の投稿記録は `posted_ids` に `(repo, item_number, comment_id)` の組で渡す。

`process_comments` の `cutover_visible` は、時系列記録に `cutover-time` が見える場合だけ真にする。時刻未確認の間と切り替え前のコメントは、`context` に入れる。要求や再開の入力にしない。文脈だけのコメントは確認済み位置を進めない。後から切り替えの記録が確認できた場合も再評価できる。

`positions` は、repo・項目番号・コメント ID の複合キーから確認済みレコードへの辞書である。返された辞書を既存の checkpoint に保存し、次の呼び出しへ渡す。本文編集は `revision` の変化で再審査する。日付だけのカーソルに置き換えない。審査で拒否した版は確認済みにするが、要求には加えない。受付数の超過分や不完全なレコードは未処理に残す。

`admit(comment)` は必須である。`AdmissionGate` を使う原子的な受付関数を渡す。受付キーにはコメント ID と版を含める。`state: ready` 以外ではコメントをモデルへ送らない。

情報待ちの項目は提起者の新しい安全な回答だけで `resume: true` を返す。危険な回答や別人の回答では情報待ちを続ける。完了済みの項目では `new_work: true` を返す。閉じた Issue も同じ処理に渡し、再開は収集側の `reopen` で行う。質問や期限の記録には既存の台帳・カード API を使う。

## 要求と受付数

要求は `{id, source, text, status, evidence}` で保存する。`source` には repo、項目番号、コメント ID を含める。ID は出所・本文・同文の出現番号から UUID5 で作る。同じ出所の同じ要求は、順序が変わっても ID を保つ。編集や削除で古い要求を自動で消さない。取り下げを含む解決は証拠を付けて明示的に完了させる。

`status` は内部では `pending|done`、`evidence` は証拠参照の文字列配列である。要求一覧が空のままではマージを許可しない。質問だけの経路は要求照合とマージを通さず、調査と回答で完了する。

`AdmissionGate` の保存口は `transact(callback)` を持つ。callback に受付済みレコードの配列を渡し、読み取り・判定・追記を一つの原子的処理として永続化する。既存の収集・台帳のトランザクションに接続し、別の常駐サービスは作らない。レコードは `{key, author, kind, at}` である。再起動後も復元し、上限超過を待機に残す。既定値は `axcommon.defaults` から読む。

## 検証手順

`python3 -m py_compile` で `safety/` と担当試験の Python ファイルを確認する。担当 pytest は `python3 -m pytest -p no:cacheprovider tests/b14/safety` で実行する。偽のモデル・受付保存口・注入検査は `tests/fakes/b14/safety/ports.py` にある。共通 fixture が socket 接続を拒否する。

実環境では、S-8 の独立会話とツール禁止、モデルの期限と巡回予算、受付トランザクションの永続性、台帳・カードの期限後再開、時系列の切り替え記録、GitHub のコメント全件取得を確認する。サービスの起動や実際の投稿は、この部品の単体試験に含めない。

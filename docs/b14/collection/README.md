# 変更受付の収集口

この部品は S-19.1、S-19.2、S-19.7 の収集・状態照合・優先順位・予算を扱う。
GitHub クライアント、権限照合、外向け検査を呼び出し側から渡す。常駐処理やネットワーク接続は含まない。

## 対象と保存先

`plugins/subloop/change/targets.yaml` は F-6.1 の5リポジトリだけを登録する。
`issue_repo` と `implementation_repo` は別々に記録する。曖昧な対応や設定不足は、その対象だけを止める。
対象を増やすには設定への明示的な登録が要る。
`test_recipe` は b14-3 の `verification/recipes.yaml#名前` を参照する。
hibi-server の `verify_runner` は `env:hub-host-local:hibi-server-e2e` である。

`priority_labels` は先頭ほど優先する。既定は空で、同順位の項目は受付順に進む。
対象別の `execution_cap` は既定で1とした。`outbound_allowlist` は空である。
依存取得先の許可は登録簿に従って別途設定する。空の一覧を通信の許可として扱わない。
公開側の翻訳 PR の条件は `contribution` に置く。他の対象は外部の貢献を受けない。

`CollectionStore` には保存先を明示する。sl-change の `AX_DATA_DIR` の下の `collection.sqlite` を使う。
ホスト名、アドレス、資格の値は設定に含めない。保存ファイルは権限600で新規作成する。
SQLite の一意制約と `BEGIN IMMEDIATE` で、収集・着手・予算・排他の判定を原子的に行う。
これはサブループの復旧用の記録である。時系列記録や台帳の API を置き換えない。

## Python の入口

`plugins.subloop.change.collection` から次の型を読み込む。

| 型 | 用途 |
| --- | --- |
| `ItemKey(provider, issue_repo, kind, number)` | S-19.2 の項目キー。kind は issue または pr |
| `Snapshot(key, version, labels, external_state, body, implementation_pr)` | 収集した版。version は内容の変化を区別する識別子 |
| `TargetRegistry.load(path)` | 対象設定の読み込み。不正な対象は errors に残す |
| `CollectionStore(path, clock=...)` | 収集・着手・外部操作・コメント確認位置の保存 |
| `CollectionService(store, registry, client, ...)` | 収集、復旧、受付、ラベル、コメントの処理 |
| `Observation(state, external_id)` | 外部で確認した S-3.4 の状態 |

項目キーの repo 名は小文字にそろえる。GitHub の大文字・小文字の差で別の項目を作らない。
実装先の PR は `Snapshot.implementation_pr` に `{repo, number}` として渡す。
公開側の番号から実装側の PR 番号を推定しない。

## 巡回と着手

1. `store.begin_cycle(cycle_id, mode=..., dry_run=..., change_starts_per_cycle=4, change_model_calls_per_cycle=30, change_cycle_seconds=300)` で巡回を記録する。同じ ID を再利用しても予算は戻らない。
2. `service.collect(cycle_id)` で明示した対象の開閉両方の項目を集める。戻り値は targets、discovered、errors、dry_run を持つ集計である。
3. 再起動時は `service.reconcile_external()` を先に呼ぶ。`service.claim_next` も最初の呼び出しで照合を行う。
4. `service.claim_next(cycle_id, owner, lease_seconds=60, paths_by_key=...)` で一件ずつ確保する。戻り値が None なら起動しない。
5. モデルを呼ぶ直前に `store.consume_model_calls(cycle_id, count)` を呼ぶ。False なら呼び出さず、残りを待機させる。
6. 外部の作業を起動する直前に `store.validate_lease(claim)` で所有を確かめる。起動 ID を受けたら `store.record_submission(claim, external_id)` に記録する。
7. 所有中は `store.renew(claim)` で期限を延ばす。終了を確認して `store.finish(claim, state, reason=...)` を呼ぶ。

予算の knob の権限は `BUDGET_KNOBS` の `who: [inspection, user]` とする。
制御 API からの有効値を `begin_cycle` に渡す。サブループが上限を引き上げる入口は作らない。
期限や予算に達した未着手分は `planned` に残る。mode は run、collect_only、poll_paused、draining を受ける。
`collect_only` は収集と記録だけを行う。dry_run でも着手と外部への書き込みを行わない。

同じ実装 repo の更新は、パスが違っても重ねない。
既定のパスは実装 repo の名前で区切るため、別 repo の `**` は互いを止めない。
共通のファイルを扱う場合は `paths_by_key` に共通の識別パスを渡す。値の鍵は `ItemKey.encode()` である。
このパスは排他のための識別子であり、コマンドや許可パスを変更する入力ではない。
競合した項目は期限を付けて待機する。他の対象は続ける。

## 復旧とチェックポイント

外部操作の前に `launching` を保存する。投稿 ID や作業 ID が確定したら記録する。
再起動時は、記録済み ID で外部状態を確認する。確認できない操作は `lost` とし、自動再送しない。
不明な作業は repo とパスの確保を維持する。その作業がまだ書き込み中である可能性があるためである。
記録済みの作業 ID について後から終了を確認できたら、確保を解く。無関係な repo は待たせない。
外部 ID が一度も保存されなかった作業は、自動で確保を解かない。

`save_checkpoint(claim, verified_results=[...], data={...})` は確認済み結果の参照と対象版を保存する。
`resume_checkpoint(key, version=..., verified_results=[...])` は対象版、実装先、確認済み結果が一致した場合だけ data を返す。
作業中に収集版が変わったら、古い結果を新しい版の確認結果として保存しない。
再収集だけで完了済み項目を再開しない。`reopen(key, authorized=True)` は安全審査と要求照合で再開を決めた呼び出し側だけが使う。
`lost` の再送には使えない。closed の項目も収集するが、収集だけでは着手しない。

## コメントとラベル

`receipt(cycle_id, key, body)` は項目と受付世代の識別子を付けて一度だけ送る。
AX が保存した投稿 ID だけを照合する。旧ワークフローの投稿を探さない。
応答が失われて ID が不明なら `lost` とする。推定して再投稿しない。
`update_rank(cycle_id, key, position)` は確認済みの受付投稿を更新し、順位が同じなら書き込まない。
`sync_labels(cycle_id, key)` は保存した現在の状態と外部ラベルの差だけを同期する。他者のラベルは残す。

受付コメントとラベルの直前には、渡された `permission_check(repo, operation)` を呼ぶ。
操作名は S-20.3 の `comment` と `label` である。コメント本文は `outbound_check(text)` も通す。
これらを渡さなかった場合は書き込まない。継続許可の一覧から不可逆操作の許可を推定しない。

`comments(key, cutover_time=...)` は context と new を返す。コメントは安全審査前の入力である。
切り替え時刻が未確認なら、全件を context に置く。時刻が確認されたら、その後の新しい人間のコメントだけを new に置く。
アカウント名だけでは自動投稿と判定しない。AX の記録済み投稿 ID と提供元の origin を使う。
コメントには repo、item_number、comment_id、author、created_at、revision、origin を付ける。
GitHub アダプタは revision と origin を含む完全な情報を返す。情報不足を安全な入力に変換しない。
安全審査側が処理した版だけを `store.acknowledge_comments(key, comments)` に渡す。
明示した確認まで再配送する。編集された版は再び new に出る。closed のコメントも同じ口で取得する。

## GitHub アダプタ

`models.GitHubClient` の各関数を実装したオブジェクトを渡す。
一覧はページを最後まで読み、通信と外部作業の照合には期限を設ける。
`list_items` の state は all である。読み取った別 repo の項目を返しても、対象には加えない。
`observe_work` は所有者と作業 ID を外部で確認する。確認不能を running や completed として返してはならない。
アダプタの例は `tests/fakes/b14/collection/github.py` にある。

b14-4 の `CollectionPort.collect(dry_run=..., mode=...)` と `checkpoint(item, state, reason)` は別の内部インターフェースである。
配備側の factory がこの部品に接続し、確保と予算の判定を通してから項目をグラフに渡す。
`CollectionService.collect` の集計を項目一覧として渡してはならない。
この部品の S-3.4 の状態と、統合側の queued、retry_wait、succeeded などの経路名を混同しない。

## GitHub の資格の複製手順

この手順は F-3.2 の配備時に行う。この成果物を作る間は実行しない。

1. 操作先の profile から secrets_dir を確かめる。元はそのホストの `~/.config/gh/hosts.yml` とする。
2. Python で github.com の oauth_token を読む。無ければ users のログイン名の下の oauth_token を読む。値を標準出力やログに出さない。
3. secrets_dir の `github-token` にトークンの文字列一行だけを書く。権限は600とする。hosts.yml 全体は複製しない。
4. manifest の secrets と同名の秘密を、使うプラグインにだけ読み取り専用で渡す。ワーカーには渡さない。
5. コンテナでは GH_TOKEN に読み込む。gh の設定ファイルは作らない。ログイン名だけを取得して所定のアカウントと照合する。
6. 元の資格を更新したら同じ手順で書き直し、使うコンテナを再起動する。

## 検証

T-20 と T-21 は `tests/b14/collection/` にある。共通の conftest がネットワーク接続を拒否する。
偽 GitHub、偽時計、SQLite の一時ファイルだけを使う。

| 配備後に確かめる項目 | この成果物での扱い |
| --- | --- |
| profile の保存先と volume を使った再起動・所有権 | 未実行。実サービスと profile が必要 |
| F-3.2 の資格の複製とログイン名の照合 | 未実行。実際の資格を扱うため |
| 実 GitHub でのページング・投稿 ID・ラベル差分の照合 | 未実行。外部アクセスを行わないため |
| b14-4 の factory と時系列記録・台帳への接続 | 統合担当で確認する。担当外のコードは変更しない |

# サブループの作成と登録

S-6.21 の判定と登録処理を実装する。入口の差し込み口は `subloop.register(app)` だ。共有の行き先定義と親パッケージは変更しない。

## 入力と承認

内部の `Request` は、入口の検証を終えた依頼を表す。公開の payload の形式を追加するものではない。既存の `schemas/dest/subloop.create.json` は `x-ax-unresolved` のままで、公開入口は受け付けない。

`spec` の必須宣言は `secrets`、`outputs`、`sensitivity` だ。秘密と外部操作が空で、情報区分が `public` のときだけ `operate` とする。それ以外は承認待ちになる。司令塔の受け口では、4〜2000 文字の `quote` を必須とする。本文の `tier`、`approved`、`approval_id` は権限の根拠にしない。

`Ports.approval` は入口の承認保存先に接続する。返す `Approval` は、保存済みの内容と利用者本人の承認を照合した結果でなければならない。外部から受け取った JSON をこの型へ直接変換してはならない。`operate` の席が外部操作を依頼した場合も、S-6.21 と T-217(c) に従って承認待ちとする。

## 接続手順

1. `Store` に入口専用の保存先を渡す。日ごとの受付数、依頼、ブランチ、ToDo、完了後の処理段階を SQLite に保存する。既定の作成数は1日3件、登録総数16件だ。日付は `env_use_day_tz` の既定 `Asia/Tokyo` で区切る。HQ の依頼は日ごとの上限から除く。登録総数には HQ の依頼も含む。
2. `Ports` に台帳、承認、登録簿、候補ブランチの読み取り、制御 API の検査、事象の保存を渡す。`create_todo(record)` は `request_id` で冪等にし、カードへ `created_by` と `quote` を記録する。候補ブランチの読み取りは、共通部品が取り込む場所とは別の、変更できない保存先を返す。
3. `CommonBridge` の `submit_item` と `accept_branch` を `Ports` に渡す。`Service` には `stage=bridge.plan_registration` を渡す。この組み合わせは、共通部品のスナップショットを選んだ後まで登録ファイルを書かない。`common.submit_item(app, item, approval_id=..., metadata=...)` と `common.accept_branch(app, item_key, before_accept=...)` を使う。
4. `app.state.b12_subloop` に `Service` を設定する。公開の入力形式が決まった後で、`app.state.b12_subloop_decode` に `Request` への変換を設定する。変換は宣言と `quote` を変更してはならない。変換やサービスが未設定なら `NotImplementedError` で止める。
5. b12-1 の後で `subloop.register(app)` を呼ぶ。共通の時系列購読が既にあれば、この行き先の `item.state` だけを接続する。他の作業や中核変更の事象は元の処理へ渡す。新しい待受や購読ループは起動しない。

## 組み立てと取り込み

ToDo を作ってから、`kind: subloop_create`、`role: impl`、`branch: subloop/<id>`、`template: templates/subloop/` の項目を渡す。`acceptance` には例と境界の両方が必要だ。`inbox_full` なら `parked` とし、1時間後を再確認の時刻にする。

完了事象から manifest の内容やパスを採用せず、候補ブランチのファイルを読む。manifest と graph を制御 API の `POST /v1/validate` と同じ形式で検査する。登録の直前にファイルをもう一度読む。宣言を超えていれば登録ファイルを書かず、ブランチを残して `manifest_exceeds_declaration` で `parked` にする。承認済みの HQ の出し直しは、このブランチを再利用する。

共通部品の `before_accept` でも、実際に選ばれたスナップショットを検査する。合格した場合だけ、registry の断片、生成した `registry.yaml`、`compose.d/sl-<id>.yaml` を同じコミットに入れる。コミットには `created_by`、`quote`、承認があれば `approval_id` を記録する。共通部品は、このコミットを検証してから取り込み、受け入れのタグと引数なしの deploy 要求を作る。

`plugin.registered` と `range_review` のカードにも出所を記録する。カードには「司令塔 <seat_id> が作成」と調整範囲を載せる。カードの保存に失敗した場合は、同じ完了事象の再処理で続ける。取り込みや deploy は繰り返さない。

## 検証と接続条件

担当試験は `python3 -m pytest -p no:cacheprovider tests/b12/subloop` で実行する。共通の fixture がネットワーク接続を拒否する。偽の入口、work-tdd、登録簿、台帳、git、deploy を使う。本物の共通エンジンとグラフ検査器を呼ぶ試験も含む。

公開入口全体の試験には、payload の仕様確定と b12b2 の段判定の接続が必要だ。現行の `AuthorityPolicy.check` は段を引き上げた後に席の上限と比較するため、外部操作を宣言した `operate` の席を承認待ちへ進める条件の調整が必要になる。この担当の内部判定は S-6.21 の明示条件に合わせている。

実機のビルド、起動、healthz、ホスト見張りへの要求受け渡しは、移行時の試験で確かめる。この部品は実機を直接操作しない。

# X の投稿案と送信

送信の処理 ID は `x-posts` とする。投稿案を作る日次グラフは `x-posts-draft` とし、どちらもこのフォルダのノードを使う。日次グラフも送信側の `x-posts` の実効 mode を確認する。開始指示の前は統計と材料だけを保存し、ワーカーを起動しない。

## グラフの登録

| 登録するファイル | 処理 ID | 予定 |
|---|---|---|
| `manifest.yaml` | `x-posts` | 600秒ごと。`every_s` は S-17 の `u_x_queue_interval_s` に対応する |
| `draft.manifest.yaml` | `x-posts-draft` | 毎日07:00 Asia/Tokyo |

B7 の cron は UTC で動くため、日次グラフの初期値は `0 22 * * *` とする。日次時刻を変更するときは、再起動の前に `runtime.prepare_manifest(manifest, effective_knobs)` を呼ぶ。`u_x_draft_time` を UTC の cron に変換する。反映していない時刻では日次の巡回を拒否する。予定を逃した分は追いかけない。

両方の manifest を B16b の登録簿に載せる。compose と登録簿は共通担当が組み立てる。この部品はポートを追加しない。

## 実行時の接続

1. このフォルダの `__init__.py` を Python のパッケージとして読み込む。フォルダ名にハイフンがあるため、`importlib.util.spec_from_file_location` の `submodule_search_locations` にこのフォルダを指定する。
2. `ControlAdapter` に既存の制御 API のクライアントを渡す。対象は `GET /v1/plugins/x-posts` の `delivery_targets`、mode は既存の effective API から読む。許可は共通の `PermissionReader` が送信の直前に確認する。さらに既存の命令一覧と許可一覧を読み、直近の利用者の開始命令の `permission_ids` と `source_instruction` に一致する許可だけを使う。別の許可を登録しただけでは送信の範囲を広げない。条件の評価ができない許可は拒否する。制御側の状態ファイルをマウントしない。
3. `XEnvironment` に B14b2 の `env.run` の接続を渡す。`EnvironmentRunner.run(request, artifacts)` は内部の Python の接点であり、新しい HTTP API ではない。添付を既存の成果物の経路に登録し、依頼を投入し、終了結果を `EnvResult` に変換する。
4. `XPosts` にワーカー、公開検査、`EventSink`、lost の台帳への報告関数を渡す。`runtime.RuntimeBinding` を作り、B7 の `Services.x_posts_binding` に設定する。両グラフで同じ永続ディレクトリを使う。
5. 起動時に `XPosts.recover()` を呼ぶ。送信の排他を取れないときは復旧を進めない。終了時に `close()` を呼ぶ。

`UnboundEnvironment` は接続前の部品であり、呼ぶと `NotImplementedError` になる。環境、ワーカー、検査の接続が無いときに、送信の成功を返してはならない。

`RuntimeBinding.accounts` には登録済みの送信先 ID と X のアカウント名の対応をプロファイルから渡す。ホスト名やアドレスをコードに埋め込まない。対応の無い対象は起動しない。`queries` は登録した材料の検索条件を渡す。

## 環境と資格

収集は `x-collect`、送信は `x-post` を能力 `x-session` の `env.run` で呼ぶ。収集と送信は資源側で選ばれた同じ経路を使う。この部品は経路 A と B の切り替えや資格の移動を行わない。資格の値やファイルを読まず、ワーカーへ渡さない。返信の引数も持たない。

`EnvResult.output` は、収集では取得した統計または材料、送信では `result.json` の内容とする。送信を確認できるのは、登録したアカウントの投稿 URL が返ったときだけである。終了コードの成功だけでは送信済みにしない。`definitely_not_sent` は、環境側が未送信を確認できた場合だけ真にする。

`EnvironmentRunner.lookup(job_id)` は、元の送信ジョブの照合結果を同じ型で返す。receipt が無い、照合できない、応答が失われた場合は `unknown` のままにする。新しい投稿を照合の代わりに実行しない。

## 投稿案と検査

ワーカーには統計と材料を渡す。生成結果は `Draft` の列とし、`draft_id`、本文、期限、添付を持つ。対象・期間・`draft_id` が同じ案は、最初に登録した内容を保持する。

検査は公開条件、秘密、個人情報、yomiyasu の4つを確認し、`Inspection` で各結果を返す。4つとも `passed` の案だけをキューへ入れる。不合格は `rejected`、検査失敗は `check_error` として保存する。形式が不正な案は別の表に保存する。送信の直前にも同じ必須検査を行う。

## 永続状態

`x-posts.sqlite3` に統計、材料、投稿案、キューを保存する。ファイルの権限は600とする。本文と添付はこの私有の記録に置き、事象には入れない。`delivery.sqlite3` は共通の `DeliveryJournal` が持つ。送信のキーは処理 ID、送信先 ID、候補 ID の組である。

候補 ID は `ax-x-<ULID>` とする。旧キューの日時と名前の形式を使わない。起動時に旧の `queue/`、`posted/`、`held/`、`results.tsv`、Discord の旧カーソルを探索しない。再起動前に未送信だった古い候補は除外する。未送信が確定した試行済みの候補は保持する。

1回に送る候補は1件とし、同じ宛先への送信は3時間以上空ける。送信を始める前に `launching` を保存する。確実な未送信は3回まで試行し、その後は `held` にする。結果不明は `unknown` として lost を報告し、照合まで再送しない。

## 担当試験

`tests/b16b/x_posts/run-checks.sh` は担当ファイルの `py_compile` と担当の pytest だけを実行する。既存の uv キャッシュを読み、一時物は `$TMPDIR` に置く。キャッシュの拡張モジュールが別の Python 版の場合は `AX_PYTHON_ABI` を指定する。パッケージは導入しない。

T-106 の資格の移動、権限600、排他、実際の認証は B14b2 と B17 で確認する。旧キューの現物の形式と候補 ID の記載は、実機を扱える段で共通担当が確認する。`docs/CHANGELOG.md` には `ax-x-<ULID>`、旧形式との違い、確認日時、確認した対象を記載する。

## 設定と運用

X の投稿キューを処理し、別のグラフで投稿案を作ります。

manifest の `schedule` は `{"kind": "interval", "every_s": 600, "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `every_s` | `600` | 投稿キューの確認間隔（秒） |
| `enabled` | `true` | 自動実行 |

manifest の秘密の識別子: なし。これらは環境変数名ではありません。

登録後、制御の `run_now` で実行を要求できます。`set_mode` で `draining` を指定すると新しい実行を止められます。再開には対応する mode を指定します。制御要求の受付は実行完了を意味しません。

投稿を扱います。送信側の既定 mode は `collect_only` で、送信には開始命令と許可が必要です。

試験: `tests/b16b/x_posts/`。

`draft.manifest.yaml` の `u_x_draft_time` は `07:00`、`enabled` は `true` が既定です。

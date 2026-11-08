# Discord 転送

`DiscordForwarder` は、AX の記録だけを使って X の投稿を Discord に転送する。X 投稿キューには依存しない。

## 呼び出し方

ハイフンを含む配置名のため、`__init__.py` を `importlib.util.spec_from_file_location` でパッケージとして読み込む。試験の読み込み方を参照する。

1. b16b-1 の `TargetRegistry` を使い、`load_targets(config_path, registry)` で送信先を読む。
2. `UserPostsSource(runner, decoder=decoder)` を作る。`runner.run(request, artifacts)` は既存の `env.run` の呼び出し口につなぐ。結果の `state` と `output["posts.json"]` を受け取る。
3. `PermissionReader(control_client)` と `DiscordWebhook` を作る。秘密は `axcommon.creds.read_secret` が読む。送信先ごとの秘密の名前は `discord-webhook-<id>` とする。
4. `DiscordForwarder` をグラフの起動時に1回作る。巡回ごとには作らない。`state_dir` はこの処理が所有する AX の永続領域にする。
5. 制御 API が返した mode で `tick(mode=mode)` を呼ぶ。初期値は `collect_only` とする。`permission.change` と `set_mode(run)` の2段階は b16b-1 が検証する。送信側から許可を作らない。

`secret_scan(text)` は公開前の秘密検査の呼び出し口である。検査に通った場合だけ `True` を返す。例外と他の戻り値は送信を拒む。`emit(kind, subject, data)` と `lost(record)` は時系列記録と台帳につなぐ。投稿の本文と webhook の値はこれらの呼び出し口に渡さない。

## 設定と状態

`config/discord-targets.yaml` は、対象の配列か、`targets` に対象の配列を持つオブジェクトを読む。各項目は FACTS F-11.8 の7項目だけとする。project と repo は期待 channel ID に一致する b16b-1 の送信対象から取得する。未知の宛先、複数のスコープ、返信の有効化は拒否する。

初回の待ちは120秒、巡回間隔は300秒とする。制御側の `u_forward_start_delay_s` と `u_forward_interval_s` をコンストラクタに渡せる。予定を逃しても巡回を追いかけない。

`forward.sqlite` は候補と送信カーソルを持つ。`delivery.sqlite` は b16b-1 の `DeliveryJournal` が持つ。`collect_only` は候補だけを保存し、配信キーと送信カーソルを作らない。起動時刻以前の未送信候補は送らない。起動前に送信を試みて確実に失敗した候補は保持する。旧カーソルと旧送信記録は読まない。

webhook の channel ID を GET で確認し、その直後に許可を再確認する。POST は確認した URL に `wait=true` を付けて1回だけ行う。応答の channel ID とメッセージ ID が確認できた場合だけ `sent` とする。成否不明は `unknown` として台帳へ出し、再送しない。中断時の `launching` も再起動時に `unknown` へ変える。

## 接続に必要なもの

FACTS F-11.8 と ENV-MODULE §14.1 は `posts.json` のファイル名までを定め、xactions の JSON のフィールドを定めていない。取得した実物を秘密を除いて確認し、`Post(id, created_at, text, retweet, reply)` に変換する decoder を渡す。decoder が無い場合は `NotImplementedError` とし、その対象の取得を失敗として扱う。JSON のフィールド名を推測しない。

親パッケージで、manifest・グラフ・ノードからこの呼び出し口を接続し、送信対象の登録と mode の適用を行う。再試行の期限と上限、`unknown` の外部照合も親の実行系に接続する。この部品は確実な送信失敗だけを再試行の対象として返す。

## 実環境での確認

実環境の確認は未実行である。この成果物を作る段階では、外部アクセスとサービスの起動が禁止されている。

| 対象 | 確認すること |
|---|---|
| T-66 | 実際の `x-user-posts` の取得結果から decoder を作り、期待 channel ID と webhook を確認する |
| T-67 | 実際の応答でメッセージ ID を保存し、照合できない送信は再送しない |
| T-153 | 利用者の開始指示と permission_ids を通し、許可の無効化後に送信しない |
| T-155 の担当分 | グラフの実プロセスを再起動し、起動時刻以前の未収集投稿を除外する。旧記録は読まない |

# プラグイン一覧

manifest を持つディレクトリの一覧です。詳細は各 README を参照してください。

- [resources](resource/resources/README.md): モデルの登録情報と状態を管理します。
- [worker-pool](resource/worker-pool/README.md): ワーカーのジョブを受け付け、モデルを選んで実行します。
- [change](subloop/change/README.md): Issue と PR を収集し、変更の検証とマージを進めます。
- [release](subloop/release/README.md): 公開待ちの項目をまとめ、製品の登録情報に従って公開します。
- [work-tdd](subloop/work-tdd/README.md): 実装依頼をテスト駆動の手順で進め、ブランチを用意します。
- [conversation-index](timer/conversation-index/README.md): 会話を伏せ字にして索引を作ります。
- [entry-index](timer/entry-index/README.md): プラグインと Compose サービスの登録情報を集め、一覧を保存します。
- [hibi-state](timer/hibi-state/README.md): 環境ジョブで Hibi の状態を取得し、検証して ledger に保存します。
- [memory-digest](timer/memory-digest/README.md): 記憶を読み、伏せ字にして日別とプロジェクト別の要約ファイルを作ります。
- [scheduled-publisher](timer/scheduled_publisher/README.md): 収集した対象について判断し、許可された操作を実行します。
- [security-watch](timer/security-watch/README.md): セキュリティ監査を環境ジョブとして実行します。
- [tumu-watch](timer/tumu-watch/README.md): Tumu の監視を設定されたタイマークライアントに依頼します。
- [x-posts](timer/x-posts/README.md): X の投稿キューを処理し、別のグラフで投稿案を作ります。

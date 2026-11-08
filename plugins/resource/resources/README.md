# resources

モデルの登録情報と状態を管理します。

## 設定と運用

manifest の `schedule` は `{"kind": "interval", "every_s": 300, "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

manifest に `knobs` はありません。

manifest の秘密の識別子: `jev-key`。これらは環境変数名ではありません。

Compose のサービス `resources` として起動します。停止・再開は配備の Compose 構成で行います。依存サービスへの影響を確認してください。

外部のモデル情報を取得し、Jev に問い合わせる処理があります。実行条件は配備と設定によります。

試験: `tests/b6a/`。

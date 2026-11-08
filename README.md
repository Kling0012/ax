# AX

AX は、AI への依頼を受け付け、作業の実行と状態の記録を扱う自律運用基盤です。Docker Compose で動く中核サービスと、ホスト上のスクリプト、プラグインで構成します。

初めて使う方は [導入手順](docs/manual/getting-started.md) を参照してください。設定から起動、状態の確認までを説明しています。各文書の一覧は [マニュアル](docs/manual/README.md) にあります。

## はじめ方

1. Docker Engine、Docker Compose、Python 3.12 以上、uv を用意します。
2. `templates/profile/profile.template.yaml` をコピーし、接続先と状態の保存先を設定します。秘密の値は profile や Git に書かず、`templates/secrets/README.md` に従って用意します。
3. [導入手順](docs/manual/getting-started.md) に従い、設定の検査とイメージの準備を行います。
4. `host/ax-up.sh --profile <profileのパス> observer intake timeline ledger` でサービスを起動します。これは一部のサービスの起動例です。依頼の実行には追加の設定とサービスが必要です。
5. `ax status` で状態を確認します。依頼と進行状況の確認は [CLI リファレンス](docs/manual/cli-reference.md) を参照してください。

日常の監視や更新は [運用ガイド](docs/manual/operations.md)、エラーの調査は [トラブルシューティング](docs/manual/troubleshooting.md) を参照してください。

## 状態

このリポジトリは `0.1.0` の骨組みです。マニュアルには実装途中の接続や `DEFERRED` と記した未実装の操作があります。ロックファイルの存在は、対象環境でのインストール、ビルド、実行の成功を示すものではありません。利用前に対象環境で確認してください。

## ライセンス

[MIT License](LICENSE)

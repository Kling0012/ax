# 管理対象の登録案内

この案内は既存の AX に対象を追加するときの入口です。値は架空の例です。登録前に対象の権限、接続先、実行経路を確認してください。各例を置いただけではサービスの起動や実行経路の完成を意味しません。

## 全体の流れ

```text
利用者 → ax / gate → intake → control → サブループ → worker / 実行環境
                           ↓               ↓
                      ledger・timeline   登録された対象
ホストの systemd timer → hostwatch → 登録された Compose サービス
```

構成とネットワークは [構成](../manual/architecture.md)、導入は [初期設定](../manual/getting-started.md) を参照してください。CLI の実装状況と結果不明時の扱いは [CLI reference](cli.md) にあります。`ax show` や `ax go` は未実装です。受付と作業の完了は別の状態です。

## リポジトリ

[リポジトリ用 Compose 例](../../templates/project/compose.override.example.yaml) は `sl-work-tdd` に読み取り専用のリポジトリを渡します。`EXAMPLE_REPO_PATH` には対象ホストの絶対パスを設定します。コンテナ内の `AX_REPO_EXAMPLE_PROJECT` と mount 先が一致し、ID は `example-project` になります。

```yaml
services:
  sl-work-tdd:
    environment:
      AX_REPO_EXAMPLE_PROJECT: /git/example-project.git
    volumes:
      - type: bind
        source: ${EXAMPLE_REPO_PATH}
        target: /git/example-project.git
        read_only: true
```

これは Compose の override です。ホストで変数を設定しただけではサービスに反映されません。適用方法と、`git.worktree`、`main`、権限、モデル、別の書き込み用 staging リポジトリの条件は [初回依頼の例](example-onboarding.md) を確認してください。staging の項目ブランチは強制 push されるため、元のリポジトリや共有の本番ブランチと分けます。この経路は end-to-end 未検証です。

## サービスと監視対象

ホスト見張りは `${ax_state_dir}/hostwatch/registered/<サービス名>` の空ファイルで、既存の Compose サービスを対象にします。これは任意の外部サービスや URL を登録する汎用監視設定ではありません。実際のサービス名は Compose の設定で確認してください。例では `observer` を使います。

```text
${ax_state_dir}/hostwatch/registered/observer
```

通常の起動手順では `host/ax-up.sh` が healthz を確認してから登録します。[初期設定](../manual/getting-started.md) と [hostwatch](../b8/hostwatch.md) を参照してください。登録されたサービスは異常時に再起動されるため、対象の選択は運用上の操作です。`hostwatch` 自身の登録印は作りません。見張り自身は `systemctl --user is-active ax-hostwatch.timer` で確認します。

## 定期処理

タイマーの雛形は [templates/plugin/timer](../../templates/plugin/timer/README.md) にあります。manifest の記入例は [manifest.yaml](../../templates/plugin/timer/manifest.yaml)、登録断片は [registry.fragment.yaml](../../templates/plugin/timer/registry.fragment.yaml) です。`schedule.every_s: 300` は 300 秒間隔の例で、`missed: record_only` は逃した実行を追いかけません。

`manifest.yaml` と `handler.py` を対象プラグインのディレクトリに置き、registry 断片と必要な secret の参照を配備設定に組み込みます。タイマーは共有の `timer-runner` が実行します。登録簿の生成と検査、handler の試験は [プラグイン](../manual/plugins.md) と雛形の README に従います。外部送信などの処理を追加する場合は、対象と再実行時の影響を確認してください。

## 実行環境

ホストや sandbox は [環境の記入例](templates.md#bringing-an-environment-under-management) と [環境用雛形](../../templates/env/README.md) を参照してください。環境の登録、接続、対応操作と権限は別々に確認します。SSH の現行範囲などは [既知の制限](known-limitations.md) を参照してください。

## 日々の確認と復旧

`ax status`、`ax todos --project example-project`、`ax events --project example-project` で状態を確認できます。各コマンドの引数は `ax --help` と `ax <command> --help`、対応一覧は [CLI reference](cli.md) を参照してください。結果不明の書き込みは元の request ID を保ち、状態を照合してから再送を判断します。

バックアップ、復元、秘密の更新は [運用ガイド](operating.md#secret-rotation-and-recovery)、障害の切り分けは [トラブルシューティング](../manual/troubleshooting.md) を参照してください。復元の実環境での検証状況を確認してください。秘密の場所と変数名は [設定一覧](../manual/configuration.md) と [セキュリティ](../manual/security.md) にあります。秘密の値はリポジトリや例に記入しません。

試験と開発環境は [開発](../manual/development.md) と [CONTRIBUTING](../../CONTRIBUTING.md) を参照してください。manifest と graph はそれぞれの検査器に通します。Compose 例の YAML 形式が正しいことと、配備先で経路が動くことは別の確認です。

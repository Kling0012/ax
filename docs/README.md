# AX 文書の入口

AX は作業依頼、実行、記録と監視を扱う基盤です。実装と配線には未完成の部分があります。導入前に [現状と制限](guide/known-limitations.md) を確認してください。

```text
利用者 → ax / 入口 → 依頼の受付 → サブループ・ワーカー
                         ↓                 ↓
                   台帳・時系列記録 ← 状態と結果
                         ↑
                     監視・制御
```

| 目的 | 文書 |
| --- | --- |
| 構成とデータの流れ | [構成](manual/architecture.md) |
| 前提、profile、秘密、起動 | [はじめ方](manual/getting-started.md)、[導入ガイド](guide/getting-started.md) |
| 日々の操作と実装状況 | [CLI ガイド](guide/cli.md)、[CLI リファレンス](manual/cli-reference.md) |
| リポジトリやサービスを管理対象にする | [管理対象の登録](guide/management-ja.md)、[登録例](guide/example-onboarding.md) |
| 設定と雛形 | [設定リファレンス](manual/configuration.md)、[雛形](../templates/)、[設定例](guide/templates.md) |
| 秘密の名前と置き場所 | [秘密の一覧](guide/secrets-ja.md)、[セキュリティ](manual/security.md) |
| 監視、ログ、復元、障害 | [運用](manual/operations.md)、[障害対応](manual/troubleshooting.md)、[運用ガイド](guide/operating.md) |
| 単体試験と全体 CI | [開発ガイド](manual/development.md)、[試験手順](test-procedure.md) |
| 実装の範囲と制限 | [現状と制限](guide/known-limitations.md)、[構成](manual/architecture.md) |

実際の導入や復元を行う前に、対象環境の設定と利用する機能の依存関係を確認してください。文書中の例は環境に合わせて置き換えます。

## その他の文書

- [CHANGELOG.md](CHANGELOG.md)
- [b0/README.md](b0/README.md)
- [b1/README.md](b1/README.md)
- [b1/archive-volumes.md](b1/archive-volumes.md)
- [b10a/README.md](b10a/README.md)
- [b10a/emergency/README.md](b10a/emergency/README.md)
- [b10a/notify/README.md](b10a/notify/README.md)
- [b10a/standby/README.md](b10a/standby/README.md)
- [b10b/api/README.md](b10b/api/README.md)
- [b12/cli/README.md](b12/cli/README.md)
- [b12/subloop/README.md](b12/subloop/README.md)
- [b12/work-core/README.md](b12/work-core/README.md)
- [b12b1/gate/README.md](b12b1/gate/README.md)
- [b12b1/seat/README.md](b12b1/seat/README.md)
- [b12b1/ssh/README.md](b12b1/ssh/README.md)
- [b12b2/hq/README.md](b12b2/hq/README.md)
- [b12b2/intake/README.md](b12b2/intake/README.md)
- [b12b2/schemas/README.md](b12b2/schemas/README.md)
- [b12b4/fakes/README.md](b12b4/fakes/README.md)
- [b12b4/guide/README.md](b12b4/guide/README.md)
- [b12b4/model-exam/README.md](b12b4/model-exam/README.md)
- [b14/README.md](b14/README.md)
- [b14/collection/README.md](b14/collection/README.md)
- [b14/integration/operations.md](b14/integration/operations.md)
- [b14/integration/ports.md](b14/integration/ports.md)
- [b14/safety/README.md](b14/safety/README.md)
- [b14/verification/README.md](b14/verification/README.md)
- [b14b1/adapters/README.md](b14b1/adapters/README.md)
- [b14b1/registry/README.md](b14b1/registry/README.md)
- [b15/README.md](b15/README.md)
- [b15/deploy/README.md](b15/deploy/README.md)
- [b15/release/README.md](b15/release/README.md)
- [b15/translation/README.md](b15/translation/README.md)
- [b16b/contracts.md](b16b/contracts.md)
- [b17/real-tests.md](b17/real-tests.md)
- [b18b/README.md](b18b/README.md)
- [b18b/box/README.md](b18b/box/README.md)
- [b18b/screenshot/README.md](b18b/screenshot/README.md)
- [b18b/winwork/README.md](b18b/winwork/README.md)
- [b3/README.md](b3/README.md)
- [b5/README.md](b5/README.md)
- [b6a/README.md](b6a/README.md)
- [b6b/README.md](b6b/README.md)
- [b6b/procedure-caller-ax-key.md](b6b/procedure-caller-ax-key.md)
- [b6b/procedure-firewall.md](b6b/procedure-firewall.md)
- [b6b/procedure-launcher-ssh-key.md](b6b/procedure-launcher-ssh-key.md)
- [b6b/real-env-checks.md](b6b/real-env-checks.md)
- [b7/MIGRATION.md](b7/MIGRATION.md)
- [b7/README.md](b7/README.md)
- [b8/README.md](b8/README.md)
- [b8/cutover.md](b8/cutover.md)
- [b8/host-scripts.md](b8/host-scripts.md)
- [b8/observer.md](b8/observer.md)
- [b8/real-env-checks.md](b8/real-env-checks.md)
- [b9/README.md](b9/README.md)
- [b9/fault-api.md](b9/fault-api.md)
- [b9/validation.md](b9/validation.md)
- [cutover/README.md](cutover/README.md)
- [cutover/interface.md](cutover/interface.md)
- [cutover/memory/README.md](cutover/memory/README.md)
- [cutover/reap/README.md](cutover/reap/README.md)
- [cutover/retirement/README.md](cutover/retirement/README.md)

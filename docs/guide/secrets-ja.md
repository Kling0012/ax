# 秘密の置き場所

profile の `secrets_dir` がホスト側の置き場所を決め、`AX_SECRETS_DIR` として Compose に渡されます。値をリポジトリやログに書かないでください。秘密の準備とローテーションは [セキュリティ](../manual/security.md)、起動の手順は [はじめ方](../manual/getting-started.md) を参照してください。

## Compose secrets の一覧

ファイルは `AX_SECRETS_DIR` からの相対パスです。Compose のファイル参照はホスト側の所有者と権限を使います。通常の `0600` は準備時の目安で、宣言自体は所有者や権限を設定しません。サービスの実行 UID が読めるように準備してください。複数の UID で共有するファイルは、アクセス方法と権限を個別に確認してください。既存ファイルは `ax-up.sh` が一般に所有者や権限を修正しません。

| 名前 | ファイル | Compose の利用サービス | 権限と所有者 | 宣言 |
| --- | --- | --- | --- | --- |
| `archive-known-hosts` | `archive-known-hosts` | `archiver` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b1.yaml` |
| `archive-ssh-key` | `archive-ssh-key` | `archiver` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b1.yaml` |
| `control-apply-token` | `control-apply-token` | `control`, `sl-change`, `sl-release`, `sl-work-tdd`, `timer-runner`, `worker-pool` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.yaml` |
| `control-token-backup` | `control-tokens/backup` | `control`, `observer` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.yaml` |
| `control-token-emergency` | `control-tokens/emergency` | `control`, `emergency` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.yaml` |
| `control-token-inspection` | `control-tokens/inspection` | `control`, `inspector` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.yaml` |
| `control-token-manager` | `control-tokens/manager` | `control`, `manager` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.yaml` |
| `core-token-control` | `core-tokens/control` | `control` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b3.yaml` |
| `core-token-gate` | `core-tokens/gate` | `gate` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b12b1.yaml` |
| `core-token-hq` | `core-tokens/hq` | `hq` | `0640`、所有者は既存のまま、グループ `AX_HQ_GID` | `compose.d/b10b.yaml` |
| `core-token-intake` | `core-tokens/intake` | `intake` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b10a.yaml`, `compose.d/b16b.yaml` |
| `core-token-manager` | `core-tokens/manager` | `manager` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b9.yaml` |
| `core-token-observer` | `core-tokens/observer` | `emergency`, `observer` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b10a.yaml`, `compose.d/b8.yaml` |
| `core-token-release-review` | `core-tokens/release-review` | `sl-release` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/sl-release.yaml` |
| `core-token-worker-pool` | `core-tokens/worker-pool` | `worker-pool` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b6a.yaml` |
| `github-token` | `github-token` | `sl-change`, `sl-release` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b14.yaml`, `compose.yaml` |
| `hq-access` | `hq-access` | `hq` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b10b.yaml` |
| `hq-allowed-users` | `hq-allowed-users` | `gate`, `hq` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b10b.yaml`, `compose.d/b12b1.yaml` |
| `jev-key` | `jev-key` | `resources` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.yaml` |
| `launcher-known-hosts` | `launcher-known-hosts` | `worker-pool` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b6a.yaml` |
| `launcher-ssh-key` | `launcher-ssh-key` | `worker-pool` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b6a.yaml` |
| `model-gateway-key` | `model-gateway-key` | `resources`, `sl-release` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.yaml` |
| `pii-list` | `pii-list` | `control`, `emergency`, `ledger`, `manager`, `memory-connector`, `observer`, `sl-release`, `timeline`, `timer-runner` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b1.yaml`, `compose.d/b3.yaml` |
| `plugin-token-change` | `plugin-tokens/change` | `sl-change` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/sl-change.yaml` |
| `plugin-token-conversation-index` | `plugin-tokens/conversation-index` | `timer-runner` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b16a-t62.yaml` |
| `plugin-token-hibi-state` | `plugin-tokens/hibi-state` | `timer-runner` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b16a-t62.yaml` |
| `plugin-token-memory-digest` | `plugin-tokens/memory-digest` | `timer-runner` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b16a-t62.yaml` |
| `plugin-token-release` | `plugin-tokens/release` | `sl-release` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/sl-release.yaml` |
| `plugin-token-security-watch` | `plugin-tokens/security-watch` | `timer-runner` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b16a-t62.yaml` |
| `plugin-token-timer-runner` | `plugin-tokens/timer-runner` | `timer-runner` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/b7.yaml` |
| `plugin-token-work-tdd` | `plugin-tokens/work-tdd` | `sl-work-tdd` | 通常 `0600`、サービスが読める所有者を要確認 | `compose.d/sl-work-tdd.yaml` |

## 自動生成と HQ のファイル

`host/ax-up.sh` は対象サービスに対応する `plugin-tokens/<id>` と `core-tokens/<部品名>` を、存在しなければ `0600` で生成します。`resources` は `worker-pool`、`emergency` は `observer`、`sl-release` は `release-review` の core token を生成対象にします。生成対象には `manager`、`intake`、`observer`、`gate`、`control`、`hq` も含まれます。プラグイン ID は登録簿から選びます。

`core-tokens/hq` は HQ の起動時に、数値の `AX_HQ_GID` があり、シンボリックリンクでなくリンク数が 1 の場合、グループを `AX_HQ_GID`、権限を `0640` にします。所有ユーザーは変更しません。`hq-proof/key` は HQ または intake の起動時に必要な ID がそろえば生成し、所有者を `AX_UID:AX_HQ_GID`、権限を `0640` にします。親ディレクトリ `hq-proof` は同じ所有者で `0710` です。証明鍵は HQ と intake に読み取り専用で渡されます。

`hq-slots` は HQ と worker-pool が bind mount で使うディレクトリです。Compose secrets のファイル一覧とは別に準備します。`launcher-known-hosts` の空の雛形はありません。`templates/secrets/` の `.tmp-placeholder` は名前を示すだけで、実際の資格ではありません。

## 関連する変数

| 変数 | 用途 |
| --- | --- |
| `AX_SECRETS_DIR` | Compose が秘密ファイルを探すディレクトリ |
| `AX_PROFILE_FILE` | 起動時に使う profile の指定。`--profile` が優先 |
| `AX_PLUGIN_TOKENS_FILE` | プラグイントークンの照合情報のホスト側保存先 |
| `AX_PLUGIN_TOKENS` | コンテナ内のプラグイントークン照合表 |
| `AX_CORE_TOKENS` | コンテナ内の core token 照合表 |
| `AX_SECRETS_DIR_HOST` | archiver が使うホスト側の秘密ディレクトリ |
| `AX_CONVERSATION_HISTORY_HOST_DIR` | 会話履歴のホスト側ディレクトリ |
| `AX_EMBEDDING_TOKEN_HOST_FILE` | 埋め込み用トークンのホスト側ファイル |
| `AX_EMBEDDING_TOKEN_FILE` | コンテナ内から参照するトークンファイル |
| `AX_SECRETS_PATH` | `read_secret` がコンテナ内で秘密を探す場所。既定は `/run/secrets` |
| `AX_GATE_CORE_TOKEN_FILE` | gate の core token ファイル |
| `AX_GATE_ALLOWED_USERS_FILE` | gate の許可利用者ファイル |
| `AX_HQ_PROOF_KEY_FILE` | HQ と intake の証明鍵ファイル |
| `AX_UID` / `AX_GID` | 通常のサービスの実行ユーザーとグループ |
| `AX_HQ_UID` / `AX_HQ_GID` | HQ の実行ユーザーとグループ |

会話履歴の取り込みには対応する設定が必要です。[運用ガイド](operating.md#conversation-indexing) を確認してください。これらの変数名は値ではありません。秘密の中身を環境変数の例や画面出力に含めないでください。

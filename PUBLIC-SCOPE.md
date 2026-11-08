# 公開の木の範囲（PUBLIC-SCOPE）

この文書は、公開候補に含める対象と除外する対象を記録します。環境固有の値、秘密、生成物は公開しません。

## 公開に含める対象

`compose.yaml`、`compose.d/`、`docker/`、`core/`、`axcommon/`、`runtime/`、`plugins/`、`schemas/`、`config/`、`host/`、`commander/`、`templates/`、`profile/example.yaml`、`tools/`、`ci/`、`tests/`、`docs/`（設計文書7件を除く）、`.github/`、`LICENSE`、`README.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`、`pyproject.toml`、`uv.lock`、`.gitignore`、`.gitattributes`、`PUBLIC-SCOPE.md` を含めます。`tools/ci-fixtures/` の値は検査用のダミーです。

## 公開から除外する対象

| 対象 | 理由 |
|---|---|
| 設計文書7件（`docs/REQUIREMENTS.md`、`SPEC.md`、`DESIGN.md`、`FACTS.md`、`ENV-MODULE.md`、`CONSISTENCY.md`、`INDEX.md`） | 設計文書は公開しないため |
| `build/`、`archive/` | 作業用の複製と退避先であり、公開物ではないため |
| `reports/` | 実行時の出力先であり、値を持つ可能性があるため |
| `.serena/`、`.code-review-graph/`、`.venv/` | 開発ツールのキャッシュと OS 依存の生成物であるため |
| `profile/example.yaml` 以外の `profile/` | 環境ごとの接続先や設定を含むため |
| `outbox/`、`*.sqlite*`、`__pycache__/` など | 実行時の状態または生成物であるため |

## 匿名化の確認

- AX の製品既定として定義した `10.201.x.x` のコンテナ用アドレスと Docker サブネットは残します。
- 公開候補を作り直した後は、`gate.sh <public-clone> public/main replacements.tsv` を実行して、作業ツリーと到達可能な履歴を再走査します。

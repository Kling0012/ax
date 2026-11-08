# CONTRIBUTING

AX への貢献に興味を持っていただき、ありがとうございます。この文書は報告・提案・変更の送り方と、このリポジトリの決まりをまとめたものです。
開発環境の用意、テストの実行、CI、道具の使い方の詳細は [docs/manual/development.md](docs/manual/development.md) を参照してください。
AX は自律運用の基盤です。安全のための決まり（秘密を置かない、外へ公開する口を増やさない、失敗は局所に止める）は、利用者の利便よりも優先します。

## 目次

- [1. はじめに読む物](#1-はじめに読む物)
- [2. 不具合の報告と提案](#2-不具合の報告と提案)
- [3. 開発環境](#3-開発環境)
- [4. 変更の作り方](#4-変更の作り方)
- [5. テスト](#5-テスト)
- [6. コミットと pull request](#6-コミットと-pull-request)
- [7. 安全のための決まり](#7-安全のための決まり)
- [8. 行動規範](#8-行動規範)

## 1. はじめに読む物

設計の正本は `docs/` の文書です。変更の提案の前に、意図がどこに書いてあるかを確かめてください。文書と違う動作を見つけたときは、どちらが正かを報告に書いていただけると助かります。

| 文書 | 内容 |
|---|---|
| `docs/INDEX.md` | 全文書の索引。節の探し方 |
| `docs/REQUIREMENTS.md` | 要求（R-*） |
| `docs/SPEC.md` | 仕様（S-*） |
| `docs/DESIGN.md` | 設計（D-*） |
| `docs/FACTS.md` | 現物の値の記録（F-*） |
| `docs/manual/` | 利用者向けの手引き（[はじめ方](docs/manual/getting-started.md)、[開発ガイド](docs/manual/development.md) など） |

## 2. 不具合の報告と提案

GitHub の Issue テンプレートを使ってください。

- 不具合: `.github/ISSUE_TEMPLATE/bug_report.yml`（ラベル `bug`）
- 機能の提案: `.github/ISSUE_TEMPLATE/feature_request.yml`（ラベル `enhancement`）

報告に秘密・トークン・個人情報を含めないでください。ログを貼る前に、値が置き換えられているか確認してください。

## 3. 開発環境

必要な物は Python 3.12 以上、uv、Docker Engine（compose プラグイン込み）です。依存は `uv sync --locked --group dev` で入れます。手順の全体は [開発ガイドの 2](docs/manual/development.md#2-開発環境の用意) を参照してください。

## 4. 変更の作り方

作業はブランチで行い、pull request として送ります。共有ファイル（`compose.yaml`、`plugins/registry.yaml`、`config/intake-destinations.yaml` など）は直接編集せず、束ごとの拡張点（`compose.d/<id>.yaml`、`plugins/registry.d/<id>.yaml` など）に書いてください。一覧は [開発ガイドの 1](docs/manual/development.md#1-リポジトリの構成) にあります。

生成物（`plugins/registry.yaml`、`docs/INDEX.md` の索引など）は手で直さず、道具で作り直します。設計文書（`docs/SPEC.md`、`docs/DESIGN.md`）を変える変更は、対応関係の検査（`python3 tools/trace_check.py docs`）も通るようにします。

## 5. テスト

- テストの書き方の正本は `docs/test-procedure.md` です。要点: 1 テスト 1 つの振る舞い、受け入れの例と境界の例の両方、外部に触れない（`tests/fakes/` の偽の部品を使う）、名前は `test_<対象>_<条件>_<結果>`。
- 変更した束のテストは `python3 -m pytest -p no:cacheprovider tests/<束の ID>` で確かめます。
- 全体は `bash tools/ci.sh`（終了コード 0 が合格）で確かめます。Docker や外部環境が必要な検査は、利用可能な条件と未実施の理由を報告してください。
- GitHub Actions の CI は `workflow_dispatch` による手動起動だけです。push や pull request では自動起動しません。起動には対象リポジトリで Actions を実行できる権限が必要です。
- 実行した検証だけを「確認済み」と書き、実行しなかった物は理由付きで「未実施」と書いてください。

## 6. コミットと pull request

- コミットメッセージは `<pkg>: <summary>` の形（例: `host: add peer registry script`）。`<pkg>` は束の ID、場所、機能の名前です。
- pull request には `.github/PULL_REQUEST_TEMPLATE.md` を使います。実行した検証と、未実施の物を正直に書いてください。
- レビューは CI の結果と試験を基本に判断します。文面の議論だけで合否を決めません。

## 7. 安全のための決まり

変更に次の物が含まれると、取り込みを断ります。

- 秘密・トークン・個人情報を git に置く変更（ダミーは `tools/ci-fixtures/` だけです）
- `gate` 以外のサービスへの `ports` の追加、または `0.0.0.0` での待ち受け（`tools/compose_check.py` が拒否します）
- docker.sock のコンテナへの追加
- 1 つの機能の失敗で全体を止める変更（fail-soft を守ってください）
- 既存のテストを、意図を変えずに消す・弱くする変更

## 8. 行動規範

このプロジェクトは [行動規範](CODE_OF_CONDUCT.md)（Contributor Covenant 2.1）を採用しています。参加するすべての人に適用されます。

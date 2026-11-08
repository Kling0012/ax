# 管理役と点検係の配置手順

`compose.d/b9.yaml` は `manager` と `inspector` を定義する。根拠は SPEC S-10.4・S-13、DESIGN D-2.1・D-12、FACTS F-14.1・F-18.1・F-18.4。どちらも中核で、制御命令の対象にしない。

## 配置と資格

両サービスは `docker/base.yaml` の `ax-core-base` を継承する。所属は `ax-core` だけで、ホストへポートを公開しない。共通の `/healthz:8080`、`TMPDIR=/tmp-hdd`、ログの上限、権限の絞り込みも継承する。ホスト名とパスは profile から作る `AX_` 変数で渡す。

| サービス | 起動入口 | 永続ボリューム | 設定と秘密の名前 |
|---|---|---|---|
| manager | `python -m core.manager` | `manager-data:/data` | `AX_MANAGER_STATE_DIR=/data`、`control-token-manager`、`core-token-manager`、`pii-list` |
| inspector | `python -m core.inspector` | `inspector-data:/data` | `AX_INSPECTOR_STATE_DIR=/data`、`control-token-inspection`、`config/inspection.yaml` の読み取り専用 bind |

管理役の宛先は `AX_TIMELINE_URL`、`AX_CONTROL_URL`、`AX_LEDGER_URL`、`AX_WORKER_POOL_URL`、`AX_RESOURCES_URL`、`AX_MEMORY_CONNECTOR_URL`。すべて中核の8080を使う。点検係は時系列記録と制御 API だけを呼ぶ。
管理役の秘密検査は `${AX_STATE_DIR}/redact` を読み取り専用で渡し、`AX_REDACT_INDEX=/redact/index` と `AX_PII_LIST=/run/secrets/pii-list` を使う。モデルのゲートウェイ鍵、Docker ソケット、他の部品のデータベースは渡さない。

`manager-data` には判断・購読位置・送信待ちを保存する。`inspector-data` には `state.json`、`history.jsonl`、`outbox/` を保存する。B1 の `archive-init` が両ボリュームの所有者を `AX_UID:AX_GID` に揃え、`archive/` を作る。`archiver` に渡すのは各 `archive/` の subpath だけである。

## 移行時の準備

1. B0・B1 と依存パッケージの配置を用意する。イメージは既存の `ax/core:${AX_TAG}` を使う。`compose.yaml`、`compose.d/b1.yaml`、`compose.d/b9.yaml` を同じ構成に含める。ledger・control・resources・worker-pool は各担当の断片で追加する。
2. profile の変数から環境ファイルを用意する。`AX_HOME` は配備したリポジトリを指す。そこにある `config/inspection.yaml` を点検係へ読み取り専用で渡す。
3. 制御用の2つの秘密を `${AX_SECRETS_DIR}/control-tokens/manager` と `control-tokens/inspection` に用意する。値は記録に出さない。管理役の台帳用資格は `core-tokens/manager` に置き、呼ばれる側の `core-tokens.sha256` に `manager` の行を登録する。権限と形式は F-18.4 に従う。
4. この資格の準備を終えてから、担当のオフライン検証を通す。手順は [検証手順](validation.md) を参照する。実環境の起動・切り替えは移行の実施者が行う。

現行の `host/ax-up.sh` は `core-tokens/worker-pool` だけを自動作成する。手順3の管理役用資格の作成・登録は担当外の未接続箇所である。未登録なら管理役から台帳への書き込みが拒否される。全体の移行前にホスト側の担当へ接続を引き継ぐ。

## 点検の流れ

点検の初期間隔は24時間、範囲は24〜168時間である。予定の30分前に予告する。全プラグインへ期限6時間の `set_mode: draining` を1つずつ出し、実行中の `runs` と `jobs` が0になるか2時間経つまで待つ。長い作業を取り消さない。

点検係は `inspection.phase` の `phase=review_request` と `plan.inspection_id` で管理役に段4〜7を頼む。別名の `inspection.review.requested` は発行しない。管理役の `manager.decision` の `step=review` と同じ `inspection_id` を受けると、点検係の命令だけを解除する。別の点検の応答は無視する。

オブザーバーの `health.alert` が `check=drain_silent`、`destination=inspection` のときは、見直しを飛ばして解除する。点検係が落ちた場合も、制御 API が `on_expiry=release` の期限で解除する。点検外の管理役の命令は解除しない。

恒久修繕と予備役の起動がない点検が3回続くと、以後の静かな点検で間隔を1.5倍に延ばす。上限は設定の範囲とする。崩れが出ると初期値に戻す。範囲を狭めて再起動したときは保存済み間隔も範囲内へ寄せて記録する。進行中の点検と予約済みの `next_at` は保つ。

## 照会と利用者の変更

`GET /v1/manager` は周回・判断・受信件数を返す。`GET /v1/inspection` は現在の段・間隔・次回時刻・前回結果を返す。両方とも中核の網の中だけで使う。

D-12 は利用者による変更の前後と理由の記録を求めるが、送り手・資格・受け渡し API を定めていない。既存の `POST /v1/review-changes` は501を返す保留の口とした。この口へ送っても変更と記録は受け付けない。利用者の変更を受ける経路は `build/OPEN.md` の未解決事項である。

点検係の内部関数 `record_user_change` は `key`・`before`・`after`・空でない `reason` を持つ記録を保存し、次の `inspection.phase=review` に載せる。この内部関数は候補や上限を変更しない。外部から呼ぶ契約が決まるまで、部品間の接続には使わない。

故障注入の部品は [故障注入 API](fault-api.md) を参照する。

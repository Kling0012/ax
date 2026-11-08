# 導入・更新・取り外しの監査の記録（雛形）

この記録は環境モジュールの導入・更新・取り外しの実行後に残します。
1 回の操作につき 1 ファイルにします。値は例で、実在の値は実行時に確かめて記入します。

## 環境

| 項目 | 値 |
|---|---|
| 環境 ID (`env_id`) | `<環境ID>` |
| 導入先 | `<ホーム>` |
| `module_version` | `<X.Y.Z>` |
| `contract` | `<MAJOR.MINOR>` |
| `install.log` の有無 | あり／なし |
| 日時（UTC） | `<日時>` |

## 導入の記録

- `install/check.sh` の結果：
- `install/install.sh` の結果：
- `install.log` の追記（最終行の要点）：
- `authorized_keys` の控え（`cp -p` の日時）：
- `authorized_keys` に足した行（1 行のみ。公開鍵の値の全文は書かない）：
- `ax-env-run version` の結果（`module_version` と `contract`）：
- `install.log` に残した `manifest.yaml` の内容ハッシュ：

## 更新の記録

- 新しい版を `releases/` に並べた日時：
- `current` の切り替え（旧 `current` の値と新 `current` の値）：
- 実行中のジョブがあったか（あったら、`--drain` の有無）：
- 戻した版（`current` を向け直した日時）：

## 取り外しの記録

- `DETACHED` を作った日時：
- `archive/detached-<日時>/` へ移した物（作業ディレクトリ、ジョブの記録、`releases/`）：
- container 種別の `compose stop` の結果（コンテナとボリュームは残した）：
- 台帳の `state` の変更（`draining` と `retired` の日時）：

## 鍵の回転の記録

- 新しい鍵を作った日時（ファイル名と権限 600）：
- 古い鍵を失効させた日時：
- `probe` と `version` の再確認の結果：

## 監査（`audit-readonly`）の記録

- `audit.tsv` の `sshd_effective` の行（`passwordauthentication no` かどうか）：
- `COVERAGE` 行の範囲（root 以外は採取の範囲）：
- 差異（基準との差異）の有無：

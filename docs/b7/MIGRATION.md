# B7 の移行確認

B7 の定義を取り込む前に、依存部品と未定義の接続を確認する。秘密の値は文書やリポジトリへ書かない。

## 依存と保存先

work-tdd は runtime の共通実装、ノードの部品庫、worker-pool に依存する。timer-runner は `runtime.subloop.store.Store` と `runtime.subloop.schedule` を使う。いずれも B7-1 の成果物を先に含める。

`sl-work-tdd-data` と `timer-runner-data` は SQLite と outbox を残す。コンテナの作り直しで保存先を取り替えない。作業置き場は `${AX_HOME}/../work`、ステージングは `${AX_HOME}/../work.git`、本番のリポジトリは `${AX_HOME}/../ax.git` である。work-tdd から `ax.git` への bind は読み取り専用にする。

## 実リポジトリの初回準備

この手順は bind の前に配備担当が実行する。`AX_HOME` は配備先の絶対パスとし、その親は既存のディレクトリとする。`sl-change` と `sl-work-tdd` は両方とも `work` と `work.git` を読み書きする。`sl-work-tdd` の `ax.git` は読み取り専用で、書き込みは intake が担当する。

1. `${AX_HOME}/..` と各親の実体、所有者、権限を `ls -ld` と `stat` で調べる。Compose の `AX_UID` と `AX_GID` で work と work.git を読み書きでき、ax.git を読み取れることを確認する。親や途中に意図しないシンボリックリンクがある場合も止める。
2. `${AX_HOME}/../work`、`${AX_HOME}/../work.git`、`${AX_HOME}/../ax.git` のすべてについて、シンボリックリンクを含めて存在しないことを確認する。一つでも存在する場合は種類、所有者、権限、Git リポジトリなら `git rev-parse --is-bare-repository` の結果を記録して止め、報告する。既存物の初期化、変更、削除、移動はしない。
3. 本番の `main` と履歴の取得元を配備担当が確認する。取得元は読み取り可能な既存 Git リポジトリとし、`git -C "$AX_SOURCE" rev-parse --verify 'refs/heads/main^{commit}'` で main を確認する。取得元が不明、main がない、権限が不足する場合は止めて報告する。`AX_SOURCE` は確認した取得元を指す。
4. 配備担当が操作する間、ほかの作成者やサービスが対象パスを使わないようにする。3つのパスの不在を直前にもう一度確認する。各パスは `mkdir` で一つずつ確保し、失敗したら直ちに止める。`mkdir -p` は対象に使わない。確保したディレクトリ以外には書かない。途中で失敗した場合も残ったものを変更せず報告する。
5. 新しく確保した `work` はジョブごとの作業ディレクトリの親として空のまま使う。新しく確保した `work.git` に `git init --bare` を実行する。新しく確保した空の `ax.git` へ `git clone --bare "$AX_SOURCE"` を実行し、確認済みの main と履歴を用意する。各コマンドの実行先はそれぞれ確保したディレクトリに限定する。例は次のとおり。

```sh
mkdir -- "${AX_HOME}/../work" || exit 1
mkdir -- "${AX_HOME}/../work.git" || exit 1
mkdir -- "${AX_HOME}/../ax.git" || exit 1
git init --bare "${AX_HOME}/../work.git" || exit 1
git clone --bare -- "$AX_SOURCE" "${AX_HOME}/../ax.git" || exit 1
```

6. 作成後は `ls -ld` と `stat` で3つの所有者と権限を確認する。`git -C "${AX_HOME}/../work.git" rev-parse --is-bare-repository` と `git -C "${AX_HOME}/../ax.git" rev-parse --is-bare-repository` がともに `true` を返すこと、ax.git の `refs/heads/main` が取得元の main と同じコミットであることを確認する。Compose 定義の bind 先と `ax.git` の `read_only: true` を照合する。確認に失敗した場合は止めて報告し、既存物を作り直さない。実サービスを起動しての確認は別途行う。

`plugins/registry.d/b7.yaml` は登録簿の断片である。ホストの登録簿生成で `plugins/registry.yaml` に統合する。timer-runner は統合済みの登録簿を読む。`runtime/timer_runner/manifest.yaml` を使い、`plugins/resource/timer-runner/` の未追跡ファイルは B7-2 の取り込み対象に含めない。

## compose と資格

timer-runner と sl-work-tdd は ax-work だけに所属する。公開の `ports` と Docker ソケットを追加しない。共通イメージ `ax/runtime:${AX_TAG}` を使う。`img-runtime` は build profile のビルド用定義で、サブループごとのイメージは作らない。

ホスト側で用意する資格の場所は次のとおりである。コンテナに渡すのは必要な資格だけにする。

| ホストの場所 | 渡す相手 |
|---|---|
| `${AX_SECRETS_DIR}/control-apply-token` | timer-runner、sl-work-tdd |
| `${AX_SECRETS_DIR}/plugin-tokens/timer-runner` | timer-runner |
| `${AX_SECRETS_DIR}/plugin-tokens/work-tdd` | sl-work-tdd |
| `${AX_SECRETS_DIR}/plugin-tokens/<定期ジョブ ID>` | timer-runner だけ |

定期ジョブを追加するパッケージは、timer-runner の secrets にその資格を足す。資格の作成とトークンのハッシュ一覧の更新はホスト側の手順で行う。B7 は既存の資格を流用しない。値と実在のホスト・アドレスを成果物へ埋め込まない。

## ローカルの確認

リポジトリの根で担当試験を実行する。共通 fixture が socket の接続を拒否する。外部サービスと待受ポートは使わない。

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/b7/test_t11_tdd.py tests/b7/test_t123_items.py
```

HTTP 層は Starlette の TestClient を使う。uvicorn の起動は偽に置き換える。依存が無ければ HTTP の試験が skip になるため、skip の理由も確認する。パッケージの導入が許可されない環境では、build の `DECISIONS.md` にある既存 pytest キャッシュの前例に従う。

compose の確認には `tools/ci-fixtures/ci.env` のダミーの値を使う。次の確認は Docker デーモンに触れない。

```sh
docker compose --project-directory . --env-file tools/ci-fixtures/ci.env -f compose.yaml -f compose.d/b7.yaml -f compose.d/sl-work-tdd.yaml config --quiet
```

## 実環境での確認

この表の確認には実環境が必要になる。成果物の作成中は実行しない。T-123 の中継、資格、台帳への反映には B3 と B12 の実装も必要になる。

| 対象 | 確認する動作 | 成果物作成時の状態 |
|---|---|---|
| runtime イメージ | 固定した依存でビルドし、work-tdd と timer-runner が起動する | 未実行。ビルドとサービス起動は禁止 |
| F-18.4 の分離 | 作業側から中核の 8080 と台帳の transition に接続できない | 未実行。実際の網が必要 |
| T-123 の入口からの中継 | intake の資格だけが項目を渡せる。inbox_full が元の ToDo に反映される | 未実行。入口と制御 API と台帳が必要 |
| T-123 の完了と排出 | 台帳が item.state を読み、元の ToDo を review または parked にする | 未実行。台帳との統合が必要 |
| work.git | 検証後のブランチだけを push し、ax.git と main に書かない | 未実行。実際の Git リポジトリが必要 |
| 定期ジョブの資格 | 各処理のトークンだけで書ける。ほかの処理のトークンでは拒否される | 未実行。実際の secrets と作業側の API が必要 |
| outbox | timeline の停止中も処理を続け、復帰後に保存済みの事象を送る | 未実行。コンテナ間の疎通が必要 |
| 定期ジョブの制御 | 個別の effective がほかのジョブを止めずに反映される | 未実行。control とタイマー群の統合が必要 |
| 期限付きタイマー | 許可された action の実行と、期限・件数の制限を確認する | 未実行。到達経路と資格が未定義 |
| 共有 runner の即時適用 | 対象を区別して /apply を適用し、/status で照合する | 未実行。対象 ID の渡し方が未定義 |

期限付きタイマーと即時適用の未定義部分、空の verify の扱いは build の `OPEN.md` に残す。これらを完了した機能として移行しない。

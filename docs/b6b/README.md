# B6b ジョブ用コンテナ層

B6 のうち、ジョブ用コンテナを起動して閉じ込める層。worker-pool と資源台帳（b6a）の下にある。
設計の正本は `docs/SPEC.md` の S-8.11、`docs/FACTS.md` の F-18.1、F-18.2、F-18.4、F-19、F-11.5。

## 物の一覧

| 場所 | 内容 |
|---|---|
| `host/env-module-min/releases/b6/` | `ax-env-run` の B6 最小版（launcher）。`bin/ax-env-run` は薄い shell、本体は `lib/ax_launcher.py` |
| `host/env-module-min/install.sh` | `~/.ax-env` に b6 を置く。`current`、`bin` の印を作る。冪等 |
| `core/egress-proxy/egress_proxy.py` | `egress-proxy`。3128 の CONNECT 許可、ゲートへの素の HTTP の中継、3425 のモデルの入口の中継 |
| `host/ax-fw.sh` | `DOCKER-USER` の規則と ufw の許可規則を作る。冪等 |
| `config/runners/*.yaml` | runner の定義。`agent-cli`、`agent-cli-{node,python,dotnet}`、`chat-json`、`verify-{node,python,dotnet,ax-ci}` |
| `config/ro-mounts.yaml` | 追加の読み取り専用マウントの登録簿（初期値は `sod-gamedata` の 1 件） |
| `runtime/images/` | 作業用イメージの Dockerfile（`agent-cli`、`verify/*`、`chat-json`）と `base-images.lock` |
| `axcommon/chat_json.py` | `chat-json` の runner のクライアント |
| `compose.d/b6b.yaml` | `egress-proxy` と、ビルド専用の `img-*` |
| `schemas/ext/b6b/` | `job-start` の依頼、登録簿、runner、`chat-json` の入出力の Schema |
| `tests/b6b/`、`tests/fakes/b6b/` | 試験と偽の docker・sudo・tailscale・ufw・iptables・ストリーム |

## launcher の約束（F-18.2）

- worker-pool は SSH の強制コマンドで `~/.ax-env/bin/ax-env-run --caller launcher` を呼ぶ。サブコマンドは `SSH_ORIGINAL_COMMAND` から読む。
- サブコマンドは `job-start <id>`、`job-status <id>`、`job-stop <id>`、`job-retire <id>`、`job-list` の 5 つ。`job-start` だけ標準入力に JSON を取る。
- 出力は 1 行の JSON。終了コードは 0 成功、2 依頼の不備、3 見つからない、4 満枠、5 取り外し済み、70 内部の失敗。
- 呼び出し元が指定できるのは、runner の名前、モデル、許可先のホスト名、時間、環境変数の名前、`target_ro`、`ro_mounts` の id、`session_id`、`resume_from`、`workdir_id`、`verify`、`tools` だけ。
  イメージ、マウントのパス、ネットワーク、コマンドは指定できない。未知の項目はすべて拒否する。
- `docker run` の引数は固定。起動した後に `docker inspect` で隔離を確かめ、違えば止めて片付け、終了コード 70（`isolation_unconfirmed`）で返す。
- ジョブが終わると、`job-status` か `job-stop` の中で終了処理を行う（ログを `work/job-<wd>/.container-<id>.log` に保存、使用量を `.usage-<id>.json` に保存、コンテナを `docker rm`、許可ファイルを `egress/archive/` へ移す）。冪等。

## 差し替え点（試験用）

launcher の環境変数: `AX_ENV_HOME`、`AX_HOME`、`AX_PROFILE`、`AX_WORK_DIR`、`AX_ENV_SUDO`、`AX_ENV_DOCKER`、`AX_JOB_NETWORK`。
SSH の強制コマンドの下では sshd が環境変数を通さない（`restrict`）ので、呼び出し元は変えられない。
`host/ax-fw.sh` の環境変数: `AX_FW_SUDO`、`AX_FW_TAILSCALE`、`AX_FW_IPTABLES`、`AX_FW_UFW`、`AX_FW_OWNER`。

## 手順書

- `procedure-caller-ax-key.md`: caller `ax` の鍵の発行（F-3.1）。
- `procedure-launcher-ssh-key.md`: `launcher-ssh-key` の生成、`authorized_keys`、`known_hosts`、`install.sh`（F-18.2）。
- `procedure-firewall.md`: ufw の規則と `DOCKER-USER` の規則の反映（F-18.1）。
- `real-env-checks.md`: 実環境でしか通せない確認の手順（T-40、T-118、T-122、T-124、T-182 の入口）。

どれも go の後に operator が実行する。B6b は実行していない。

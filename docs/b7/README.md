# B7 サブループとタイマー

B7 は共通の実行系、標準の `tdd-change`、`work-tdd`、定期ジョブの実行体を持つ。仕様は S-7、設計は D-6 と D-4.4、ネットワークの制限は F-18.4 に従う。

## 配置

| パス | 用途 |
|---|---|
| `runtime/subloop/` | サブループの scheduler、executor、inbox、保存、HTTP の受け口 |
| `plugins/nodes/` | ノードの型と `with` の Schema |
| `plugins/subloop/_lib/tdd-change.yaml` | 実装を伴うグラフが取り込む部分グラフ |
| `plugins/subloop/_lib/prompts/tdd-change/` | test_design、impl、review の依頼文 |
| `plugins/subloop/work-tdd/` | 入口から受けた実装項目を進める標準の定義 |
| `runtime/timer_runner/` | 登録済みの定期ジョブと期限付きタイマーの実行体 |
| `templates/subloop/` | manifest、graph、prompts、compose の雛形 |
| `compose.d/b7.yaml` | timer-runner と共通 runtime イメージのビルド定義 |
| `compose.d/sl-work-tdd.yaml` | work-tdd のコンテナ定義 |
| `plugins/registry.d/b7.yaml` | work-tdd と timer-runner の登録行 |
| `schemas/ext/b7/items-response.schema.json` | 項目の受付応答 |

## work-tdd の入力と結果

入口は先に ToDo を作り、`POST /v1/plugins/work-tdd/items` に `work-item.schema.json` の項目を渡す。制御 API が `sl-work-tdd:8080/items` に中継する。入口からサブループへ直接接続しない。`run_now` は項目の受け渡しに使わない。

`acceptance.examples` と `acceptance.boundaries` は両方必要になる。片方が欠けると 400、項目の形が不正なら 422 を返す。冪等キーは `item_key` で、同じキーの再送は受付済みの応答になる。受付を重複させない。

受けた項目は inbox に保存する。`run` では直後に run を作り、`draining`、`collect_only`、`poll_paused` では保存だけを行う。`run` に戻ると未処理の項目を進める。inbox が上限なら 429 `inbox_full` を返す。入口が元の ToDo を `parked` にし、1 時間後を `check_at` に入れる。

`take`、`tdd-change`、`push` の順に進む。push の宛先はステージングの `work.git` だけで、`ax.git` と main には書かない。完了と排出は `item.state` の `data.todo_id` で元の ToDo に結び付く。サブループは ToDo の遷移を呼ばない。台帳が通常の work の完了を `review` にし、排出を `parked` にする。`subloop_create` と `core_change` の取り込みは入口が仕上げる。

## tdd-change

`git.worktree` のあとに test_design、赤の確認、impl、テストの変更検査、verify、coverage、lint、types、任意の mutation、review を順に実行する。モデルが変わっても同じ `workdir_id` を渡す。会話は共有しない。同じノードへの再入場だけが、条件に合う前のジョブから再開する。

テストは `tests/ax_generated/<item>/` に置く。test_design には `docs/test-procedure.md` と受け入れ条件を渡す。impl の依頼には「テストを書かない・変えない」を添える。最初から通るテストは test_design に戻す。impl がテストを変更すると `gate.tests_unchanged` が `bug` を返す。検証の失敗は回数の上限まで impl に戻り、上限でその項目だけを排出する。review の合否にはテストと関門の結果を使う。

`coverage_cmd`、`lint_cmd`、`types_cmd`、`mutation_cmd` は取り込み側のグラフから渡す。現行のノードは空のコマンドを skip にする。任意の `verify` が空のときに、必須の赤の確認と検証を何で実行するかは未定義である。この経路を検証済みの実装として移行しない。詳しくは build の `OPEN.md` の b7-2 の項を参照する。

## 定期ジョブ

`timer-runner` は登録簿の `kind: timer` を読む。各プラグインの manifest の隣の `handler.py` にある `async def run(ctx)` を呼ぶ。`ctx` はプラグイン ID、manifest、run ID、予定時刻、部品の clients、事象を書く emit を持つ。このハンドラ形式は B7 の内部規約である。

clients は timeline、ledger、resources の作業側の口を使う。各プラグインの `/run/secrets/plugin-token-<id>` をそのプラグインの呼び出しだけに付ける。run.state とハンドラの事象も各プラグインの資格で送る。送れない事象はプラグイン別の outbox に保存する。

定期ジョブは別々の asyncio Task で動く。同じジョブがまだ動いていれば、次の予定を `missed(previous_run_active)` にする。ほかのジョブは進む。handler の例外と不正な schedule はそのジョブだけに閉じる。過去の予定を追いかけて実行しない。mode で起動できない予定も `missed` にする。

制御 API の `/v1/plugins/<id>/effective` を `control_poll_s` ごとに読む。既定は 30 秒である。timer-runner 全体と各定期ジョブの mode を別々に扱い、`every_s` の knob は次の予定判定に反映する。初回の effective を読めない対象は起動しない。照会に失敗したあとは保存済みの値を使う。mode、revision、effective、次の予定は SQLite に残す。

## 未定義の接続

期限付きタイマーの取得、action の実行、状態報告は、作業側の control:8081 の許可 API に含まれない。`ControlTimerPort` は各操作で `NotImplementedError` を返し、推測した API を呼ばない。runner はこの機能だけを停止し、定期ジョブを続ける。

共有の timer-runner に届く `/apply` の本文には対象 ID がない。個別のジョブの命令を全体の命令と取り違えないよう、資格と入力を検査したあと 501 `timer_apply_target_undefined` を返す。mode と knob は定期照会で反映する。即時適用と個別の `/status` の照合は、接続仕様の確定後に統合する。

## 新しいサブループ

1. `templates/subloop/` をもとに manifest と graph を用意する。knob の型と範囲、roles、secrets の名前、outputs を宣言する。
2. 実装を伴う処理には `include: tdd-change` を使う。ノードに timeout、循環に max_loops、失敗時の行き先を定める。
3. `python3 tools/ax_graph_check.py plugins/subloop/<id>` で検査する。検査に失敗した定義は登録しない。
4. `ax subloop create` で依頼する。定義、登録行、`compose.d/sl-<id>.yaml` を同じ変更に含める。compose の雛形の `@@ID@@` を置き換える。

## サブループ固有のノード型（node_dirs）

サブループだけが使うノードの型は、共通の部品庫（`plugins/nodes/`）に入れない。置き方は 2 つある。

- `plugins/subloop/<id>/nodes/<名前空間>/<型>.py`（規約の置き場。宣言は不要）
- `nodes.yaml` で宣言する: サブループのディレクトリ、またはその直下の 1 階層（例 `integration/nodes.yaml`）に置き、`node_dirs:` に型の置き場を相対パスで列挙する。パスはその `nodes.yaml` の置き場から相対である。

```yaml
# plugins/subloop/<id>/integration/nodes.yaml の例
node_dirs:
  - nodes
```

読む順は 共通の部品庫 → `<id>/nodes/` → 宣言された `node_dirs` の順で、同じ型名は先に読んだ物が残る。宣言は `runtime/subloop/nodes.py` の `declared_node_dirs` が読み、1 つの宣言の失敗はその宣言だけを飛ばす（fail-soft）。`nodes/` を置けないサブループ（担当パスの制約）は、自分の担当ディレクトリの中に `nodes.yaml` を置くだけで型を差し込める。宣言された置き場は `ax-graph-check` と起動時の `runtime/subloop` の両方で読まれるため、検査と実行で同じ型が見える。

調整項目と範囲は登録時に利用者へ一度示す。新しいサブループの作成は利用者の指示による。

移行時の確認は [MIGRATION.md](MIGRATION.md) に記す。

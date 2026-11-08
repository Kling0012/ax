# 新しいサブループの作り方

サブループは `plugins/subloop/<id>/` のファイルだけで作る（コードの変更は要らない。D-6.1）。

1. `manifest.yaml` を埋める。`example` を自分の ID に直し、title・knobs・roles・secrets・outputs を決める。
   - `graph.yaml` が使う `{{knobs.名前}}` はすべて knobs に宣言する。
   - 外へ影響する型を使うなら、outputs に載せる。公開する型の前には `gate.secret_scan` を置く。
2. `graph.yaml` を組む。最小の形は「巡回 → 項目ごとの処理 → 結果の記録」。実装を伴うなら、`include: tdd-change` を使う（必須）。
   - 部品庫の型は `plugins/nodes/<名前空間>/`。型ごとの `with` の Schema は各ファイルの `schema`。
   - 内側の循環には `max_loops`、すべてのノードに `limits.timeout_s` を付ける。`bug` を返しうるノードは `when: bug` の辺か `on_fail` を決める。
3. `python3 tools/ax_graph_check.py plugins/subloop/<id>` で検査する。外れたものは登録できない。
4. `ax subloop create` は現在 CLI の送信形式が未実装のため使えない。次のファイルを用意し、登録とデプロイの経路を環境ごとに確認する。
   - `plugins/subloop/<id>/`（manifest.yaml、graph.yaml、prompts/）
   - `plugins/registry.d/<id>.yaml`（登録の行）
   - `compose.d/sl-<id>.yaml`（`compose.sl.yaml.tmpl` から作る。`@@ID@@` を ID に置き換える。`ports:` は持たない）

新しいサブループを登録するとき、調整項目の一覧と範囲を ToDo カードで利用者に一度見せる（S-4.3）。

## 雛形の項目

`manifest.yaml` の `id` はサービスと登録の名前、`schedule` は巡回条件、`knobs` は既定値と範囲、`roles` は使用する役割を表す。`secrets` には名前だけを書き、`outputs` に外部へ影響する操作を列挙する。`graph.yaml` の `entry` と `nodes`、`edges` で処理順と条件を指定する。各ノードの `limits` と `on_fail` を確認する。

`compose.sl.yaml.tmpl` の `@@ID@@` を同じ ID に置き換え、必要な秘密だけを渡す。登録断片は `plugins/registry.d/` に置き、`python3 tools/build_registry.py --check` で生成済み登録簿との差を検査する。変更時の生成手順は [プラグインガイド](../../docs/manual/plugins.md) にある。例の GitHub 操作や公開先は実際の権限と行き先に合わせて確認する。

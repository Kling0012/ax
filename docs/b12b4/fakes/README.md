# 司令塔試験の固定ケースと偽部品

F-9.6 の注入10件と通常10件を `tests/fakes/b12b4/cases.yaml` に置く。`world.yaml` は各ケースの開始時に複製する。どちらも YAML として読める JSON である。

## 呼び出し口

```python
from tests.fakes.b12b4 import FakeIntake, ScriptedModel, load_cases, load_world

case = load_cases()[0]
intake = FakeIntake()
intake.reset(case, model="exam-model")
model = ScriptedModel(case, "good", faults=(429, 503))
```

`load_cases()` はケースの配列、`load_world()` は世界の辞書を返す。引数で外部の入口やファイルを指定する機能はない。受験モデル名は偽の世界の席に記録するだけで、モデルを呼ばない。

| 呼び出し口 | 結果 |
|---|---|
| `FakeIntake(world=None, registry=None, via="tailnet", max_tier=None, conflict_guard=None, read_tools=None)` | 世界と登録簿を受け取る。省略時はリポジトリの固定ファイルだけを読む |
| `intake.reset(case=None, model="exam-model")` | 世界、注入文、依頼IDの記録、呼び出し履歴を初期化する。case は辞書かケースIDを取る |
| `intake.call(tool, args)` | `status`、`body`、MCP の `content` と `isError` を返す |
| `intake.handle(method, path, body=None)` | HTTP の形を関数で受け、`(status, body)` を返す。接続や待受は使わない |
| `intake.tools_list()` | 渡された読み取りの表と、登録簿から作る書き込みの道具を返す |
| `intake.calls` | `tool`、`args`、`status`、`result` の配列。未実装の呼び出しには `unavailable` を記録する |
| `intake.world` | 現在の偽の世界。呼び出し元が原本を変更しても実行中の世界には影響しない |
| `intake.final_response` | 呼び出し元が最終返答を保存する欄。台本の実行係が書く |
| `ScriptedModel(case, mode="good", faults=())` | 台本を複製する。mode は `good`、`injectable`、`sloppy` |
| `model.respond(messages=None, tools=None)` | Chat Completions の応答を1件返す |
| `model.chat_completions(payload)` | `messages` と `tools` を上の関数へ渡す |
| `model.handle("POST", "/v1/chat/completions", payload)` | `(status, body)` を返す。偽の429と503もこの形で返す |
| `model.calls`、`model.final_response` | 呼び出し履歴と最終返答 |
| `model.reset()` | 台本、障害の列、呼び出し履歴と最終返答を初期化する |

台本は `[{tool: ..., args: ...}, ..., {final: ...}]` の配列である。`good` は `golden`、`injectable` は注入ケースの `bad` を返す。`sloppy` は最初の呼び出しの引数を1つ変更する。`required` に引数の条件があれば、その条件で調べる引数を変更する。

`faults=(429, 503)` は応答前に順番に障害を返す。`respond()` は `ModelFailure` を送出し、`status` と `body` を持たせる。障害では台本を進めない。再試行の回数や採点は試験本体が決める。

## ケースの追加の欄

F-9.6 の表にある条件を表すため、照合子の例にない欄も使う。`required[].or_tools` は代替の道具名、`required[].args_has` は必須のキー、`required[].quote_in_user` は発言中の引用を表す。`same_request_id` は同じ依頼IDを使う道具の配列である。これらは採点側が解釈する。

N-10 の `faults` は偽の入口が使う。最初の503で `t-new-1` の番号だけを予約し、同じ依頼IDの再送が成功したときに `t-new-2` を1件作る。503の結果は冪等の完了記録に保存しない。これは表で指定された最終IDに合わせるための偽の行き先の動作である。

## 入口との接続

`AuthorityPolicy`、`validate`、`Ident`、`tier_sufficient`、`IdempotencyStore` を本物の入口から import する。行き先の処理は偽の世界だけを更新する。`config/intake-destinations.yaml` と `config/intake-destinations.d/*.yaml` の `tier`、`via`、`mcp_tool`、`schema` を読む。`handler` のURLには接続しない。秘密検査は掛けず、不正な呼び出しも履歴に残す。

未確定の依存は `NotImplementedError` にする。`axcommon/ops.py` の表を渡すアダプター、b12b3 の `conflict_guard`、生成された手引き、`core.change` の Schema と operate の席から承認待ちにする条件が必要である。現在の `core.change` は `x-ax-unresolved` を持ち、全入力を拒否する。I-7 と N-6 の202、N-5 の保持の衝突、実際の `tools/list` の統合は、この担当の試験では合格としていない。

## 正式な配置との差

S-8.14 が指定する `tests/fixtures/commander-exam/` と `tests/fakes/{intake_fake,scripted_model}.py` は担当範囲外である。今回の配置は `tests/fakes/b12b4/` とする。正式な場所への配置は行っていない。

登録簿も、S-8.14 の `plugins/intake/destinations.yaml` と実装計画の `config/intake-destinations.yaml` が一致していない。ここでは実装計画の既存ファイルを読む。正式な配置の統一は、この担当では決めていない。

## 検証の範囲

担当の pytest は固定ケースと偽部品を検証する。接続、bind、listen を拒否し、ファイルの参照先をリポジトリ内に制限する試験を含む。HTTP と MCP は関数として呼ぶ。実際の待受、実モデル、結果ファイルの生成と採点、結果を有効な段へ反映する処理は未実行である。

| 検証 | 状態 | 理由 |
|---|---|---|
| HTTP と MCP の実際の待受 | 未実行 | 今回は接続と待受が禁止されている |
| T-215 の実モデルによる試験 | 未実行 | 今回は実モデルを呼ばない |
| T-214 の試験本体による全体の合否 | 未実行 | b12b4-3 との統合が必要である |
| I-7 と N-6 の202、N-5 の本物の保持判定 | 未実行 | 未確定の Schema と判定関数に依存する |
| 結果の保存と有効な段への反映 | 未実行 | この担当は固定ケースと偽部品を受け持つ |

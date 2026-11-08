# 司令塔試験とモデル条件

`tools/commander_exam.py` は S-8.14 の機械採点と結果検証を受け持つ。入口は `evaluate_model` を呼び、返された段と `redacted` を応答に反映する。モジュールの import は通信、状態の読み書き、サービスの生成を行わない。

## 公開関数

| 関数 | 用途 |
| --- | --- |
| `load_cases(path)` | 20 件のケースと、LF に正規化した本文の `cases_version` を返す |
| `matches_call` / `grade_run` | 引数の照合子、順序つきの部分列、禁止、カナリア、再送、最終文を採点する |
| `ExamSettings.load(path)` | S-17 の `commander_exam_*` を読む |
| `run_exam` | 分離したモデルと入口のファクトリーで各ケースを走らせ、結果と応答ログを返す |
| `result_sha256` / `validate_result` / `read_result` | F-9.6 の形、集計、ハッシュ、モデル名、版、期限を検証する |
| `evaluate_model` | 登録表、席の段、有効な結果から `ModelCondition` を返す |
| `model_table_change_requirement` | 表の変更に必要な段と本人承認のメタデータを返す |
| `write_result` | 明示した出力先に `.log` と `.json` を排他的に作る |

`run_exam` の `guide` には `tools.gen_commander_guide.load_bundle()` の結果を渡す。本文は `guide.agents`、版は `guide.guide_version` を使う。`cases_version` は台本の本文から求め、実行時の合成カナリアを版に混ぜない。

`model_factory(case, run_index, attempt)` は `chat_completions(payload)` または `messages(payload)` を持つ部品を返す。`intake_factory(case)` は、そのケースで初期化した `tools_list()` と `call(tool, args)` を持つ部品を返す。`call` の戻り値には `status` と `body` が必要となる。試験は道具一覧の入力スキーマをモデルへ渡す。書き込みの判定には一覧の `readOnlyHint` と、偽入口の登録簿を使う。`clock` と `now` は試験用に差し替えられる。

モデルの 429、5xx、タイムアウトは同じ実行を最大 2 回やり直す。入口から返る 503 はモデルの失敗と区別し、モデルへ返す。実行回数の集計は、最後の試行の `passed`、`failed`、`inconclusive` のいずれかに 1 回分を足す。途中のやり直しと応答全文はログに残す。上限まで続いた `inconclusive` は合格数に入らない。

全体のモデル呼び出し数は、やり直しも含めてケース数 × 回数 × ターン数を超えない。既定値では 720 回となる。同期コールバックの実行時間は戻った直後にも検査する。通信アダプター側にも、その呼び出しの残り時間に収まるタイムアウトが必要となる。

## 入口との接続

`evaluate_model` は状態を書かず、結果を読むたびにハッシュを照合する。欠落、不合格、読取不能、形式の不正、ハッシュの不一致は `exam_missing` とする。厳密なモデル名の不一致は `exam_model_mismatch`、ケースの版違い、期限切れ、未来の実行日時は `exam_stale` とする。有効な結果が複数のうち 1 件あれば使える。行の照合は最も長い前方一致を選び、同じ長さなら先の行を使う。

有効な段は席とモデルの小さい方となる。自由文を受けられるのは、登録表の `max_class: sensitive` の行だけとなる。秘密と個人情報の検査は入口で先に行う。`ModelCondition.denied_event` は運用が拒否されたときの `commander.denied` の材料を返す。時系列への書き込みと、ID・件数・状態への置換は入口の担当となる。

登録表の行を認める処理には、提供元の登録、資源台帳の `standard` 以上、能力 L3 以上の確認が必要となる。この部品は承認済みの登録表を受け取る。合格結果を作るだけでは表や席の段は変わらない。表に `exams` を足す本人承認と、`seat.tier` の承認は別に必要となる。

## 未接続の契約

| 対象 | 必要な接続と制約 |
| --- | --- |
| T-207 全体 | b12b1・b12b3 が段、自由文の置換、時系列の拒否事象に接続する。表の変更の拒否と承認表示は b12b2 が受け持つ |
| T-214 全体 | `axcommon/ops.py` の道具表、b12b3 の `conflict_guard`、未確定の `core.change` のスキーマが必要となる。HTTP・MCP の待受はこの作業では行わない |
| T-216 全体 | 入口が結果を読み取り専用で読み、拒否事象を残し、別の席が継続することを通して確かめる必要がある |
| T-215 | b17 で実モデルとループバックの模擬入口を接続する。ホストの出力先、所有者、マウント、所要時間と費用も確かめる |

配布されたケースと世界と台本は `tests/fakes/b12b4/` にある。S-8.14 の配置 `tests/fixtures/commander-exam/` への移設はこの担当では行わない。ケースは `load_cases` に明示して渡す。N-9 の合成トークンには文書の伏せ字が残っているため、実行時だけ新しい `AXEXAM-CANARY-` 文字列に置き換える。I-5 と I-10 の固定の合成カナリアも実行ごとに変える。

コマンドの `--model` と `--api` は受け付けるが、ホストの HTTP・MCP とゲートウェイのアダプターは未接続であり、`NotImplementedError` で明示する。接続先を任意に渡す引数は持たない。未確定の入口の契約も `NotImplementedError` を伝え、合格の結果を作らない。

`tests/b12b4/model-exam/` は機械採点、試験の実行、結果検証、モデル条件の単体試験となる。20 件の集計には記録専用の入口を使い、本物の判定を通した全体試験とは区別する。対応済みの `ax_work` は既存の偽入口でも確かめる。通信、実モデル、実状態領域、待受を使わず、結果の書き出しは pytest の一時領域だけで行う。

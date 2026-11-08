# 共通部の接続

この節の名前は b17 内部の約束である。AX の公開 API、スキーマ、制御命令は追加しない。実施者が確認したローカルの Python モジュールで `factory(ctx) -> Backend` を実装し、CLI の `--adapter module:factory` に渡す。import 時に実機へ触れない。承認と保持の確認後だけ factory を呼ぶ。

## Context

| 入口 | 内容 |
|---|---|
| `Context(profile, archive_dir, runner, dry_run=True, authority=None)` | `Profile.host`、`ax_state_dir`、`tmp_dir` を渡す。実在のホスト値は配置先のプロファイルから読む |
| `ctx.guard(changes=())` | `ax approval verify` と `ax claim renew cutover` を呼び直す。未承認の F-15.2 の番号、保持なし、応答不正なら拒否する |
| `ctx.run(argv, env=None, timeout=30, changes=())` | dry-run ではコマンドを実行しない。実行時は guard を先に通す。標準出力と標準エラーをログへ流さない |
| `ctx.note(subject, target, result, reason='', at=None, kind='cutover.state')` | 既存の `tools/cutover_note.py` を `python3 -I` で使う。source と cause は保持の応答から道具が写す。結果は行の ULID、dry-run では None |
| `ctx.records()` | 追記ファイルの読める行から cutover.state を読む。本人と保持確認の項目が欠けた行を使わない |
| `latest_result(records, subject)` | 最新の ts の結果を選ぶ。同じミリ秒なら後に追記された行を選ぶ |

reason は秘密の値を含まない短い英語のコードにする。principal、session、via、cause を引数で渡さない。CUTOVER_AT は cutover-time だけに付ける。長い処理の間も保持を更新し、次の操作直前に guard を通す。実機のコマンドは `CommandRunner(dry_run=False)` だけが実行する。`planned=True` を成功として扱わない。

## Backend

`BoundBackend` は action と check の関数を明示的に登録する。未登録の処理は IntegrationPending で止まる。未登録を成功に変えない。全体を一度に走らせる関数は置かず、Operator.advance(stage) で一段ずつ実行する。

- `supports(action)` は接続済みかを返す。一段に必要な接続が欠けたら、その段の変更を始めない。
- `check(name, ctx)` は実機から今確かめた真偽を返す。保存した自己申告の JSON や前回の正常を根拠にしない。
- `perform(action, ctx, **kwargs)` は完了を確認してから返す。未完了、失敗、接続不能では例外を出す。返すだけで成功を偽装しない。
- `todo(key, title, ctx)` は一定のキーでカードを作る。同じ条件を繰り返してもカードを増やさない。

段ごとの action は stages.py の ACTIONS、check は各段の `_need` にある。`go_not_withdrawn` は操作の直前にも調べる。`monitoring_normal` は S-24 の4条件をすべて満たす場合だけ真にする。`Monitoring.ok()` に採取した鮮度、サービス状態、生存時刻、監視の継続時間、警報数を渡せる。

`check_b17` は REAL_TESTS の全番号について ok、failed、not_run を返す。T-78 以外は ok のときだけ次へ進む。T-55 は実通知の HTTP 200 と非空の ID を確認する。端末への到達の回答は待たない。T-215 は本物の試験結果と費用を記録し、結果ファイルを書き換えない。

## 副パッケージ

この担当の共通部を先に使えるようにしてある。b17-2、b17-3、b17-4 は別担当が作る。完成と担当試験の確認前に、進行器から各処理を呼ぶ統合は行わない。実機用の全 Backend はまだ同梱していない。既定の CLI が拒否することを受け入れ条件にする。

| 担当 | 接続する部分 | 接続後の確認 |
|---|---|---|
| b17-2 retirement | `retire_others` と `retire_archive_to_hdd`、停止状態の check | engine.Context の execute に、プロファイルからホストを選ぶ実行器を渡す。record は ctx.note、authorize は ctx.guard に結ぶ。T-78 失敗時に他の撤去が続くことを確認する |
| b17-3 memory_split.py | `split_memory` | MEMORY.md ごとに S-24.3 の照合を行い、結果を ctx.note で残す。失敗ファイルをカードにし、他は続ける |
| b17-4 reap | `enable_reap` | `reap/common_adapter.py` の from_context に ctx を渡す。既存の停止・再開を API に足さず使う。Outcome が waiting または failed なら成功扱いしない |

retirement.run_retirement が archive-to-hdd まで停止を行う構成では、retire_others の結果を保持し、retire_archive_to_hdd では停止済みを再確認する。二度停止を発行しない。ホスト単位の失敗で後続を止める判断は retirement 側が担い、巡回の再開はこの進行器が5条件で判定する。

## 再実行と記録

ファイルの複製は同じ内容なら何もしない。内容が変わった控えは上書きしない。旧入口の除去は、すでに外れていれば何もしない。旧台帳カードのキーは cutover/old-todo に固定する。reap の一度だけの有効化は b17-4 側で確認する。

記録の追記に失敗したら、その段の次の操作へ進まない。AX 本体や別の仕事を停止しない。前の操作を自動で戻さず、現物を確認してから再開する。記録は既存の S-3.2 と S-24.1 のスキーマに従う。schemas/ext/b17/stage-result.schema.json は進行器のローカル出力だけに使う。

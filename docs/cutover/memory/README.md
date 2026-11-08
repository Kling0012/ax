# MEMORY.md の分割

`tools/memory_split.py` は S-24 手順3と S-24.3 に従い、MEMORY.md の項目を分ける。Python の標準ライブラリだけで動く。個別メモは読まず、動かさない。`--apply` を付けなければ何も書かない。

## b17-1 からの呼び出し

呼び出す側が go の検証と `cutover` の保持を済ませる。段1で複製した原本を `--backup` に渡す。原本の配置は F-5.1 の `memory-index/<プロジェクトの符号>/MEMORY.md` とする。分割ツール自身は go の発行、カードの作成、事象の送信を行わない。

位置引数は F-24 の例と同じで、順に対象、判定表、旧索引の出力先となる。出力先は対象の隣の `archive/` とし、名前は `MEMORY-old-workflow-index-<UTC日付>.md` とする。既存なら呼び出し側で `-2`、`-3` を選ぶ。明示した出力先をツールが勝手に変えることはない。

```bash
# 計画。MEMORY、ORIGINAL_COPY、OUTPUT は呼び出し側が渡すパス。
python3 -I "$AX_HOME/tools/memory_split.py" \
  "$MEMORY" "$AX_HOME/docs/cutover/memory/f24-table.txt" "$OUTPUT" \
  --backup "$ORIGINAL_COPY" --json

# 上の計画と原本を確認した後の実行。
python3 -I "$AX_HOME/tools/memory_split.py" \
  "$MEMORY" "$AX_HOME/docs/cutover/memory/f24-table.txt" "$OUTPUT" \
  --backup "$ORIGINAL_COPY" --apply --json
```

Python からは次の関数を使う。

```python
from pathlib import Path
from tools.memory_split import available_output, split_memory

memory = Path(memory_path)
output = available_output(memory)  # UTC の日付。既存なら -2、-3。
result = split_memory(
    memory,
    Path(table_path),
    output,
    backup=Path(original_copy_path),
    apply=True,
)
```

`available_output(memory, day=None)` は名前を選ぶだけで、ファイルを作らない。`split_memory(memory, table, output, *, backup=None, apply=False, day=None)` は辞書を返す。`day` は試験用の `YYYY-MM-DD` で、省略すると UTC の当日になる。

各 MEMORY.md を独立に呼ぶ。結果が失敗でも、他のファイルは続ける。`todo_required` が真なら、そのファイルの理由をカードにする。呼び出し側は S-24.1 の `subject: memory-split` の記録と、撤去の archive の README に結果を残す。README には元の場所、原本の複製の場所、UTC の実行時刻、現物の項目数、移した数、残した数、出力先、四つの突き合わせを書く。追加の判定をした場合は理由も書く。

## 結果の形

`--json` を付けると標準出力に JSON を1行で返す。付けなければ件数、`unlisted:`、四つの `OK` または `FAIL`、状態を返す。項目の本文は結果に含めない。

| キー | 内容 |
|---|---|
| `schema_version` | この結果の形式では `1` |
| `subject` | `memory-split` |
| `memory`, `output` | 対象と出力先のパス |
| `status` | `planned`、`applied`、`noop`、`blocked` |
| `exit_code` | 成功は `0`。照合、入力、書き込みの失敗は `1`。既存の出力先は `2` |
| `counts` | `n0`、`moved`、`kept`、`unlisted`、`nolink`。その実行時の現物から数える |
| `checks` | `name` と `ok` の組を四つ。入力を読めなかった場合は空 |
| `unlisted` | 判定表に無かったリンク先。元の順で、重複もそのまま |
| `nolink` | リンクが無かった項目の行番号。1から数える |
| `backup_verified` | 別の通常ファイルとして読め、対象とバイト単位で一致した場合は真 |
| `todo_required` | 呼び出し側がカードにする失敗なら真 |
| `reason` | 成功なら `null`。失敗なら下表の理由 |
| `output_created` | 今回の実行で出力ファイルを作った場合は真 |
| `staging_file` | 書き戻し前の一時ファイルが残った場合のパス。通常は `null` |

`planned` は計画だけ、`applied` は書き込み済み、`noop` は移す行が0件、`blocked` は失敗を表す。`noop` では、既存の出力先と最初の原本を再び渡しても何も書かない。

| `reason` | 扱い |
|---|---|
| `verification-failed` | 書き戻さず、旧索引も作らない。カードにする |
| `output-exists` | 出力先を上書きしない。別名を選んで計画をやり直す |
| `backup-required` | 実行に原本の複製が渡されていない |
| `backup-not-independent` | 複製が無い、通常ファイルではない、対象と同じファイル、またはリンクである |
| `backup-mismatch` | 複製と現物が違う。元の複製を上書きせず、違いを確認する |
| `memory-not-regular` | 対象が通常ファイルではない、またはシンボリックリンクである |
| `output-directory`, `output-name` | F-24 の出力先と違う |
| `tmpdir-required` | `TMPDIR` が未設定である |
| `atomic-replace-cross-device` | `TMPDIR` と対象が別のファイルシステムにある。書き込まず、このファイルだけ保留する |
| `source-changed` | 計画後に現物が変わった。書き戻さず、結果にある作成済みファイルを保持する |
| `archive-verification-failed` | 書いた旧索引との照合が失敗した。現物は書き戻さず、出力を保持する |
| `io-or-input:<例外の種類>` | 読み込み、入力、書き込みで失敗した。秘密を出さないため例外の本文は返さない |

作成済みの旧索引や一時ファイルは、失敗時にも消さない。`output_created` と `staging_file` を確認し、元のまま保管する。自動で上書きして再試行しない。

## 判定と保持

`f24-table.txt` は F-24 の二つの表を取り出したものだ。主プロジェクトは119項目で、移す43、残す76となる。別のプロジェクトの1項目も含むため、ファイル全体では120項目となる。列は `ファイル名 移す|残す` の二つだけとする。相反する判定が重複した表は拒否する。

行頭が `- [` の行だけを項目として数える。リンク先で表を引き、同じリンク先の複数行にもそれぞれ判定を使う。表に無い項目と、リンクの無い項目は残す。表にあるが現物に無い項目は無視する。`archive/` へのリンクも残す。新しい項目を移す場合は、実施者が F-24 の基準で判定し、理由を README に書いて、この作業用の表に加えてから呼ぶ。原文の設計文書は編集しない。

移す行は元の順で旧索引に書く。残す項目、見出し、説明、空行を保持する。`## Archived` があればその節に1行足し、なければ末尾に作る。別の見出しが続く場合も、その手前にリンクを置く。

四つの突き合わせは表の参考件数に依存しない。移した数と残した数の合計、移した行が原本にあったこと、書き戻す本文に移した行が無いこと、残す行の完全な一致を確かめる。原本の重複行は行数も含めて扱う。重複しているだけでは失敗とは限らない。

残す最終項目に改行が無く、リンクの追加によってその行の改行が変わる場合も突き合わせの失敗にする。対象を勝手に整形して進めない。

## 原本と書き戻し

実行時の原本は対象と別の通常ファイルで、バイト単位で一致している必要がある。シンボリックリンクとハードリンクを複製とは認めない。既に分割済みで移す行が0件なら、原本を取り直さず `noop` を返す。

一時ファイルは `TMPDIR` に作る。そこへ書いて同期した後、旧索引を排他的に新規作成し、内容を照合する。その後に対象を `os.replace` で置き換える。対象の権限は引き継ぐ。旧索引の権限は `0600`、新しく作る `archive/` は `0700` とする。

`TMPDIR` と対象が別のファイルシステムの場合、原子的な置き換えと「一時ファイルは HDD に置く」という制約を同時に満たせない。ツールはこの場合に無書込で拒否する。対象の隣へ一時ファイルを作る回避は実装しない。実移行では配置の確認が必要となる。

## T-151 の確認範囲

すべて一時ディレクトリ内の複製を対象とする。共通の `tests/conftest.py` がネットワーク接続を拒否する。通常の試験では、F-24 のファイル名と中立の本文から119項目を作る。現物の複製の試験には、`TMPDIR` 内に採取済みのスナップショットを `AX_MEMORY_T151_SNAPSHOT` で渡す。変数が無ければ、この試験だけ省略する。実際の記憶の場所を試験から探索しない。

| 試験 | 確認する内容 |
|---|---|
| T-151(a) | 119項目を43と76に分け、四つの突き合わせが通る |
| T-151(b)〜(d) | 表に無い項目、表だけにある項目、リンクの無い行を扱う |
| T-151(e) | 既存の出力先を拒否し、呼び出し側は別名を選べる |
| T-151(f) | 2回目は無書込で、見出しが重複しない |
| T-151(g) | 四つの照合にそれぞれ故障を注入し、失敗した対象と並行する別の対象が独立に進む |
| T-151(h) | 別のプロジェクトの1項目を移す |
| T-151(i) | 残す行、見出し、空行、CRLF、順序、個別メモを保持する |
| 補足 | 原本の欠落と不一致、リンク、表の不備、書き戻しの中断、現物の変化、別ファイルシステムを拒否する |

実移行では、全 MEMORY.md の列挙、原本の複製と配置、go と保持、README と `cutover.state` の記録、失敗のカード化を b17-1 で確認する。これらはツール単体の模擬試験には含めない。

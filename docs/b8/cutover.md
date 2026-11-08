# 切り替えの記録（S-24.1）

## 追記の道具

`python3 -I tools/cutover_note.py <種類> <subject> <target> <result> [reason]`

- 種類は `cutover.state`、`removal.state`、`removal.check`。記録先は `AX_CUTOVER_DIR`（既定 `~/.local/state/ax/cutover`）。
- 書く前に `ax claim renew cutover --json --request-id <ULID>` を呼ぶ。席の ID・接続の ID・受け口・保持を確かめた事象の ID は、その応答だけから写す。
- 終了コード: 0 追記した / 2 引数が不正（行が 4000 バイト以上を含む）/ 3 `ax` が使えない・応答が読めない / 8 保持を持たない。2、3、8 は何も書かない。
- `subject` が `cutover-time` の記録は、環境変数 `CUTOVER_AT` を `data.at` に入れる。
- 試験は偽の `ax`（`tests/fakes/b8/fake_ax.py`）を PATH の先頭に置く。本物の `ax claim` との接続（T-208）は b12b3。

## 取り込み

オブザーバーが `observe_s` ごとに `/state/cutover/cutover-state.jsonl`（読み取り専用）の未取り込みの行を `POST /v1/events` で送る。

- 取り込み済みの行数は `/data/cutover.cursor`。無くなれば先頭から全部送る（同じ `id` は時系列記録が無視する）。
- 送る前に S-15.3 の秘密検査（`data` と `subject`）を通す。
- 壊れた行・許可外の `kind`・`source.principal`/`session`/`via`/`cause` の欠落や形の違いは取り込まず、`error.classified`（`artifact.bug`、code `cutover_record_invalid`）を書く。他の行は続ける。
- 時系列記録に届かない間は、その行から先を次の周期に回す。書きかけの末尾（改行なし）も次の周期に回す。
- 切り替えの時刻は `core/observer/cutover.py` の `latest_cutover_time()`（`cutover-time` のうち `ts` が最も新しい 1 件の `data.at`）。変更サブループが使う。

## 追記時と取り込み時の確認

`ax` の終了コードが 8 なら、出力が空でも保持なしとして終了する。JSON でない応答や形の違う応答では、何も追記せず終了コード 3 を返す。
既存の記録先でもディレクトリを 0700、ファイルを 0600 に直して追記する。`CUTOVER_AT` は `cutover.state` の `cutover-time` だけに使う。
秘密検査を用意できない間は、その行からの取り込みだけを保留する。観測とファイルへの追記は続く。

`cutover-time` に `CUTOVER_AT` がない、UTC のミリ秒表記でない、日付が不正な場合は、終了コード 2 で追記しない。取り込み側でも同じ時刻の形を確かめる。

# 司令塔の手引きの生成

`commander/GUIDE.md` を正本とする。`tools/gen_commander_guide.py` は標準ライブラリだけで形式ごとの本文を作る。ネットワーク、サービス、モデルを使わない。

## 入力と形式

`commander/targets.yaml` は JSON の構文で書いた YAML 1.2 の文書である。標準ライブラリで読めるよう、この構文に限る。対象を増やすときは `targets` の配列に行を足す。

| 対象 | ファイル | 形式 | 追加する文 |
|---|---|---|---|
| `agents` | `AGENTS.md` | `plain` | なし |
| `claude` | `CLAUDE.md` | `plain` | 末尾に <EMAIL>` |
| `gemini` | `GEMINI.md` | `plain` | なし |
| `cursor` | `.cursor/rules/ax-commander.mdc` | `mdc` | 先頭に frontmatter |
| `stdout` | なし | `agents` と同じ | なし |

各行には `name`、`file`、`format` を書く。`harnesses` と `install_hint` は導入先を検討するための情報であり、生成器は端末の設定を変えない。置き場所は導入時に各端末で確かめる。
`prepend` と `append` は文字列の配列である。取り込みの記法は Claude の行だけに許す。初期の表では本文を取り込まず、末尾の <EMAIL>` だけを使う。`mdc` の行には `frontmatter.description` と `frontmatter.alwaysApply: true` が要る。

`config/targets.yaml` は作らない。PLAN のこの配置と、S-22.9・F-9.5 の `commander/targets.yaml` の配置が食い違うためである。二重の正本を作らず、食い違いを build の `OPEN.md` に残した。

## 生成と確認

リポジトリの根で使う。以下は一時領域に生成する例である。

```sh
python3 tools/gen_commander_guide.py --output-dir "$TMPDIR/ax-guide-preview"
python3 tools/gen_commander_guide.py --output-dir "$TMPDIR/ax-guide-preview" --check
python3 tools/gen_commander_guide.py --target agents
python3 tools/gen_commander_guide.py --target claude
python3 tools/gen_commander_guide.py --version
```

既定の出力先は仕様の `dist/commander/` である。この担当の配置権限には含まれないため、今回そこへは生成していない。一時領域で同じ生成処理を確認した。導入先の設定への配置も行っていない。

`--target` と `--version` は標準出力だけを返す。ファイルを作らない。`--check` は既存の出力と、正本から作り直した内容をバイト単位で比べる。欠けたファイルや古い内容を見つけても書き換えない。生成は表にあるファイルだけを扱い、余分なファイルを削除しない。

終了コードは、成功が 0、`--check` の不一致が 1、入力や引数の不備が 2 である。秘密らしい値を検出しても、その値を標準エラーへ出さない。

## 版の印

`guide_version` は、正本の本文を LF にそろえた UTF-8 の SHA-256 の先頭12桁である。末尾の改行も本文に含む。時刻、乱数、入力の絶対パスは含めない。形式の表だけを変えても、正本の版は変わらない。

```text
<!-- ax-guide-version: <12桁>; generated from commander/GUIDE.md; do not edit -->
```

この印を生成物の先頭に置く。MDC では frontmatter の直後に置く。

## b12b3 と b12b4-3 へのインターフェース

```python
from tools.gen_commander_guide import load_bundle, render_guide, guide_version

bundle = load_bundle()  # commander_dir=Path(...) で入力先を変えられる
version = bundle.guide_version
agents_body = bundle.agents
claude_body = bundle.render("claude")
assert bundle.render("stdout") == agents_body
assert render_guide() == agents_body
```

`load_bundle` は同じ読み込みから版と本文を返す。`bundle.targets` は対象名から本文への辞書、`bundle.files` は相対ファイル名から本文への辞書である。本文には版の印も含む。`guide_version(body)` は文字列から版だけを計算する。

b12b3 はこの版と本文を `ax guide`、`GET /v1/read/guide`、`brief.guide_version` へ接続する。この担当では CLI・HTTP の応答の形を独自に決めない。他担当のファイルも編集しない。

## 担当試験

T-206 の生成、版、MDC、Claude だけの追記、正本の安全規則、更新の検出と、T-207(g) のモデル条件を `tests/b12b4/guide/` で確認する。試験は共通 fixture のネットワーク禁止を使う。

`validate_help(body, help_text)` は本文中のコマンドとオプションを help の文字列と照合する。仕様のコマンドを並べた偽の help で正常時と不一致時を確認する。CLI からは取得済みの help を `--help-file FILE` で渡せる。`help_text` は `ax --help` と各 `ax <command> --help`（ネストした `ax <command> <sub> --help` を含む）を連結した物でよい。引数の `allow_missing` は、明示した範囲で欠けを許す。既定は空で、何も許さない。

実際の `host/ax --help` との照合は `host/ax` があるときだけ担当試験で行う。`ax brief` と `ax decide` は b12b が実装する操作で、`--new-session`（S-6.19）と `--basis`（S-16.8）の引数は担当試験の明示した例外として扱う。b12b が実装したら例外を外す。`ax guide --version`、`ax guide` と生成物の一致、`ax brief` の版の更新は b12b3 との接続後に確認する。実環境への配置と読み込みも未実行である。

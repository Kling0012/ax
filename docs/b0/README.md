# B0 の生成スクリプト

B0 が作った生成物の元。手で直さず、元を直して作り直す。

| ファイル | 作る物 | 使い方 |
|---|---|---|
| `gen_schemas.py` | `schemas/*.json`（`dest/` と `ext/` を除く） | `python3 docs/b0/gen_schemas.py schemas`（リポジトリの根で実行） |
| `gen_defaults.py` | `config/defaults.yaml` | `python3 docs/b0/gen_defaults.py docs/SPEC.md config/defaults.yaml` |

- `schemas/` の検査は `python3 -m axcommon.schemas --check`（`tools/ci.sh` の(1)）。
- 各束が自分のスキーマを足すときは `schemas/ext/<束の ID>/` か、自分の名前の新しいファイルに置く。`gen_schemas.py` は B0 の分だけを作り直す。
- `schemas/` の `$id` は `https://ax.invalid/schemas/<ファイル名>`。ファイル間の `$ref` は相対で書く。

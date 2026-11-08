# node の雛形

`handler.py` を `plugins/nodes/<namespace>/<type>.py` へコピーする。
ファイル内の `schema` が graph の `with` を検査する Schema であり、別の Schema ファイルは読み込まれない。
`test_handler.py` は対象束の `tests/` 以下へ移し、実際の型名と境界条件を確認する。

この種類に manifest、registry 断片、compose overlay はない。Node registry が名前空間とファイル名から型を発見する。

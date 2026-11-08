# destination の雛形

`registration.fragment.yaml` を `config/intake-destinations.d/<bundle>.yaml` へ、`payload.schema.json` を `schemas/dest/<name>.json` へコピーする。
`__init__.py` と `handler.py` は `core/intake/dest/<bundle>/` に置く。`register(app)` が in-process handler を登録する。

この種類にプラグイン manifest、`plugins/registry.d` の断片、compose overlay はない。`handler` の `in-process` 値は同じ変更で Python handler を必ず登録する場合にだけ使える。handler がないとき、intake はその値を URL として扱おうとする。

新しい標準 destination は、現時点では `tools/check_dest_schemas.py` の固定集合にも実装として追加する必要がある。`test_destination.py` は対象束の `tests/` 以下へ移す。

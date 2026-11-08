# timer の雛形

`manifest.yaml` と `handler.py` を `plugins/timer/<id>/` へコピーする。
`registry.fragment.yaml` は `plugins/registry.d/<bundle>.yaml` へ統合し、`compose.fragment.yaml` は既存の `timer-runner` service に secret を追加する。

timer は独立した service を持たない。共有の `timer-runner` が manifest を読み、`handler.py` の `async def run(ctx)` を呼ぶ。handler は実行してよい処理だけを実装し、再実行と失敗からの復帰を考慮する。

`test_handler.py` は対象束の `tests/` 以下へ移す。`example-timer` を実際の ID に替える。

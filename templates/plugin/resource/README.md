# resource の雛形

`manifest.yaml` と `handler.py` を `plugins/resource/<id>/` へコピーする。
`registry.fragment.yaml` は `plugins/registry.d/<bundle>.yaml` へ、`compose.fragment.yaml` は `compose.d/<bundle>.yaml` へ統合する。

この handler は `/healthz`、`/status`、`/apply` を持つ最小の起動例である。`/apply` は `control-apply-token` を要求するが、effective の定期照会、状態保存、業務 API は実装していない。登録前に対象 resource の仕様に合わせて補う。

`test_handler.py` は対象束の `tests/` 以下へ移す。`example-resource` を実際の ID に替える。

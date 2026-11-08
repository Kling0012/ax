# 要求ファイル

コンテナからホスト見張りへの頼み方（S-11.6、F-18.3）。置き場は `${ax_state_dir}/host-requests/`（コンテナでは `/state/host-requests/`）。

## 形

1 要求 = 1 ファイル `<ULID>.req`。中身は 1 行の JSON（`schemas/ext/b8/host-request.schema.json`）。

```json
{"op": "restart", "name": "ledger"}
{"op": "deploy"}
{"op": "seat-sync"}
```

- 書けるのは `restart` が `observer` と `emergency`、`deploy` と `seat-sync` が `intake`。ワーカーとサブループは `/state` を見られず、書けない。
- イメージ名、タグ、パス、サービスの定義は書けない。キーは `op` と `name` だけ。
- 書き方は一時ファイル（先頭が `.`）に書いて rename する。書き手は `core/observer/hostreq.py` の `write_request()` を使うとよい。

## 受け側の規則

- 読むのは通常のファイル、512 バイト以下、所有者が自分の物だけ。シンボリックリンクは `rejected`。
- 実行するのは、操作が `restart`・`deploy`・`seat-sync` で、`restart` の名前が「登録済み」かつ「compose のサービス」かつ司令塔の名前でない要求だけ。それ以外は結果 `rejected` を記録し、`host-requests/rejected/` へ移す。
- 処理した要求は `host-requests/done/` へ移す。削除しない。
- 同じ周期の同じ要求は 1 回だけ実行する。1 周期に読むのは最大 50 件。
- `sudo -n` が失敗している間は、要求を読んでも実行せず、置いたままにする。
- `seat-sync` は `host/ax-seat.sh sync` を実行する。そのスクリプトが無い間は結果 `unavailable`。

## 書き手の検査

`build_body`、`parse_body`、`write_request`、`pending` は既存の呼び出し形を使う。`deploy` と `seat-sync` に `name` は付けない。`null` も拒否する。サービス名の末尾の改行も拒否する。
書き込みに失敗した一時ファイルは削除しない。受け側は `.` で始まる名前を処理しない。

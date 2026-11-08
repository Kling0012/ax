# ax CLI と読み取り中継

`host/ax` は入口を呼ぶ。ホストで作業や制御を実行しない。Python 3 の標準ライブラリだけを使う。

## 接続先

ローカルの接続先は次の順で決まる。

1. 環境変数 `AX_SOCKET`。
2. `~/.config/ax/config.yaml` の1行の `socket:`。
3. `~/.local/state/ax/commander/commander.sock`。

呼び出すたびに接続し、応答後に閉じる。`AX_GATE_URL` を設定すると tailnet の `gate` を使う。トークンは `AX_GATE_TOKEN`、または所有者が実行ユーザーで権限600の `~/.config/ax/gate-token` から読む。値を出力しない。

`host/ax --gate` は `host/lib/ax_gate.py` の `main(argv)` を呼ぶ。ファイルが無ければ未実装として終了コード1を返す。SSH のコマンドをシェルで実行しない。席と gate の実体は b12b1 が受け持つ。

## 基本操作

```sh
ax status
ax todos --state open --project example --subloop example --due 2026-11-08
ax events --kind health.alert --limit 10 --json
ax work --project example --title '変更を調べる' --role research
ax answer todo-example '回答' --quote '利用者の発言の原文'
ax control sl-example set_mode '{"mode":"draining"}' --reason '点検' --lease 30
```

`control` の args は JSON のオブジェクトで渡す。期限は指示の `expires_at` に置く。B5 が制御 API へ引き渡すため、同じ依頼 ID で再送しても payload の hash は変わらない。`exclude_resource` の期限は args の `until` で指定し、`on_expiry: release` を付ける。この操作では `--lease` を併用しない。

`work` の依頼 ID は行き先と payload の正規形から作る SHA-256 とする。状態を変える操作では ULID を作る。`--request-id` で上書きできる。通信が切れて結果不明になったときは、表示された ID で結果を確認する。自動で再送しない。

`--session` と `AX_SESSION` は指示の `session` に渡す。`--agent` と `--model` は引数として受けるが、送信形式が未確定のため、指定されたら未実装として終了する。環境変数 `AX_AGENT`、`AX_MODEL` も同じ扱いとする。

`subloop create --spec FILE` と `core change --spec FILE` は本文の形式が未確定である。既存の `schemas/dest/` にも `x-ax-unresolved` があるため、送信しない。brief、席、承認、判断、claim などの後続操作も、実体を持つ担当による拡張が必要である。未実装の操作で指示を送らない。

## 出力と終了コード

`--json` と `--format json` は JSON を返す。既定の `auto` は標準出力が端末なら表、それ以外なら JSON を返す。`AX_FORMAT` でも指定できる。出力には `op`、`request_id`、`say`、`data` と注意文を含める。外部の文章を含みうる行は `[外部]` と表示する。上限を超えた応答は `data.truncated=true` と再照会のコマンドを返す。

| コード | 意味 |
|---|---|
| 0 | 成功。部品の一部が unavailable の場合も含む |
| 1 | その他の失敗、未実装 |
| 3 | ソケットか gate に接続できない |
| 4 | 承認待ち。実行されていない |
| 5 | 書き込みの結果が不明 |
| 6 | 段が足りない、許可外 |
| 7 | 席が未登録、失効、停止、期限切れ |
| 8 | 指示の衝突 |

## 中継の登録

b12-1 の `register(app)` から `core.intake.dest.b12.read_relay.register(app)` を呼ぶ。同じアプリへの登録は1回だけ行う。B5 本体と compose の変更は不要である。

| 照会 | 中継先 |
|---|---|
| `GET /v1/read/status` | control の plugins と active commands、timeline の plugins・runs・controls の投影 |
| `GET /v1/read/todos` | ledger の todos。state・project・subloop・due だけを渡す |
| `GET /v1/read/events` | timeline の events。after・kind・subloop・project・subject・limit だけを渡す |

中核の網の8080番への GET だけを使う。書き込みの資格を付けず、events の `wait` も渡さない。各呼び出しは `read_relay_timeout_s` で打ち切る。部品内の照会が失敗したら、その部品を `unavailable` とし、他の部品の結果と HTTP 200 を返す。

応答のキーは control・timeline・ledger とする。status の control.data は plugins・commands、timeline.data は plugins・runs・controls を持つ。todos と events の data は中継先の応答そのものを持つ。スキーマは `schemas/ext/b12/cli/read-relay.schema.json` に置く。

照会は登録簿、冪等キー、指示や事象の記録を使わない。受け口が確かめた commander の身元で受け、B5 の監査ログに読み取りを残す。監査の集計は b12b3 が受け持つ。GET 以外は405、未登録のパスは404、HQ や身元の無い要求は403とする。

## 移行時の確認

実サービスを起動できる環境では、次の順に確認する。この成果物を作る試験では、実サービスと実ソケットを使わない。

| 試験 | 確認する内容 |
|---|---|
| T-115 所有者と権限 | commander ディレクトリは司令塔ユーザー所有で0700、ソケットは0600 |
| T-115 他ユーザー | 他ユーザーからの接続が拒否される |
| T-115 コンテナ | 内部コンテナにソケットが渡らず、入口へ接続できない |
| T-115 再作成 | intake の再作成後も、ホストの CLI が再び接続できる |
| T-115 部分停止 | 中継先の一部を止めても HTTP 200 と他の結果が返る |

オフラインの受け入れ試験は `tests/b12/cli/` に置く。偽の transport と ASGI の直接呼び出しを使い、ポートや Unix ソケットを待ち受けない。ソケットの権限と実際の接続拒否は、この試験の合格だけでは確かめられない。

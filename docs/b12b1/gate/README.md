# tailnet gate

`gate` は tailnet の端末から AX の HTTP と MCP を受ける入口である。待ち受けは `${AX_BIND_ADDR}:${AX_GATE_PORT}` だけにする。コンテナは `ax-core` だけに入り、固定アドレスは `192.0.2.13` とする。

`/healthz` は資格を確認せず `{"ok":true}` を返す。ほかの要求は Host、Origin、送信元 IP、`peers.json`、許可利用者、Bearer トークン、席、`revoked.d/` の順に確認する。`X-Forwarded-For` と自己申告の Tailscale ヘッダは端末の識別に使わない。

トークンと登録コードは intake が発行して保存する。gate は値を保存せず、登録コード、ダイジェスト、公開鍵を応答、例外、`repr` に出さない。トークンを返すのは、登録交換の応答だけである。

## 配置

`compose.d/b12b1.yaml` は次だけを gate に渡す。

- `${AX_STATE_DIR}/gate/` を読み取り専用で `/run/gate`
- `${AX_STATE_DIR}/seat/seats.yaml` を読み取り専用で `/run/seat/seats.yaml`
- `core-tokens/gate` と `hq-allowed-users` を個別の compose secret
- `config/` を読み取り専用で `/config`

`secrets_dir` 全体は mount しない。`host/ax-peers.sh` は `tailscale status --json` から `peers.json` を更新する。`host/ax-gate-revoke.sh <seat_id>` は `${ax_state_dir}/gate/revoked.d/<seat_id>` を作り、次の要求からその席を拒否する。両方とも go の後に実行する。

## gate から intake への中継

gate は検査した事実だけを `intake:8082` に渡す。中継先は `ax-core` の内側に限り、接続元の固定アドレスと `core-tokens/gate` の一致を intake がもう一度確かめる。

| ヘッダ | 値 | 扱い |
|---|---|---|
| `X-AX-Gate-Token` | gate 専用の core token | 必須。intake だけが照合する |
| `X-AX-Device-Id` | `StableID` | 必須。席の端末結び付けに使う |
| `X-AX-Device` | 端末名 | 必須。記録用の端末名 |
| `Authorization` | `Bearer <seat token>` | 通常の要求で必須。登録コードの交換では送らない |
| `X-AX-Agent`、`X-AX-Model`、`X-AX-Session` | 自己申告 | 任意。権限を上げる判断には使わない |
| `X-AX-Request-Id` | gate が作る相関 ID | 必須。応答と監査を結び付ける |

intake はこれらの資格ヘッダが欠けた要求と、同じ資格ヘッダが複数ある要求を拒否する。HTTP 本文や外部から渡された `from`、`principal`、`tier` は身元の根拠にしない。

`POST /v1/enroll` は `{"code":"<128-bit hex>"}` だけを受ける。gate は `POST /v1/seat/enroll` に写し、intake は最初に交換した端末の `StableID` にコードを結び付ける。登録コードは一度だけ使え、トークンはこの応答だけで返す。

## HQ と席の接続

席の登録時は、HQ と承認の入口が共通の `hq_verifier(scope, body)` を seat の登録にも渡す。検証器は tailnet 上の許可利用者、直接入口、第二要素の結果を返す。`seat.add`、`seat.enroll_start`、`seat.tier`、`seat.unlock`、`approval.grant` は第二要素が無いと拒否する。

`seat.add` は HQ 専用であり、payload は次の 5 項目に限る。

| 項目 | 内容 |
|---|---|
| `label` | 席の人が読む名前 |
| `device` | tailnet の端末名 |
| `device_id` | 端末の `StableID` |
| `public_key` | 1 行の SSH 公開鍵 |
| `tier` | `read`、`operate`、`irreversible` |

intake は `seat_id`、期限、`key_fp` を自分で作る。`key_fp`、`created_by`、トークンのダイジェストは payload から受け取らない。HQ が使えない非常用の `host/ax-seat.sh add` は既存どおり `read` の SSH 席だけを記録する。

席の失効、期限切れ、lockdown は、`deny_pending`、`release_controls`、`release_claims`、`close_sessions`、`seat_sync` の効果を永続的に待機させる。各所有者は先の 4 つのコールバックを登録し、失敗した効果は完了として扱わずに再試行する。`seat_sync` が未指定のとき、intake は `${AX_STATE_DIR}/host-requests/` に `{"op":"seat-sync"}` を書く。

## `peers.json`

`host/ax-peers.sh` は `peers.json` を次の形で原子的に置き換える。

```json
{
  "<tailnet IP>": {
    "device": "<tailnet name>",
    "device_id": "<StableID>",
    "os": "<OS>",
    "user_login": "<Tailscale login>",
    "online": true
  }
}
```

この形は gate の `Identities.peer()` と `schemas/ext/b12b1/gate/peers.schema.json` の必須項目に一致する。script は Self と Peer の行を同じ形で出すが、gate は `${AX_BIND_ADDR}` に当たる Self の IP を拒否する。`user_login` は `/run/secrets/hq-allowed-users` の許可一覧と照合する。

## 実機で確かめること

go の後に、別端末からの接続で送信元 tailnet IP が gate まで保たれること、再起動後の到達性、firewall の経路を確認する。必要になったときだけ `network_mode: host` の代替を検討する。`ax-peers.sh` の 30 秒 timer はこの束の書き込み範囲外であり、まだ作らない。

# intake・gate・HQ の API 境界

intake は 4 つの受け口で要求を受け、Unix ソケットでは接続ごとの `SO_PEERCRED` で身元を決めます。本文や HTTP ヘッダーだけで身元を名乗ることはできません。
gate は Tailnet の接続元と seat token を検査する状態を持たない中継です。認証済みの事実だけを intake へ渡します。
HQ は承認、席、秘密入力の画面境界です。intake へは Unix ソケットだけで接続します。未確定の wire 仕様は明記してあります。

## 目次

- [共通規則](#共通規則)
- [intake](#intake)
- [gate](#gate)
- [HQ](#hq)
- [未確定事項と HEAD との差](#未確定事項と-head-との差)
- [設定値と環境変数](#設定値と環境変数)

## 共通規則

この文書は境界層だけを扱う。ledger、timeline、control などの業務 API は [api-core-services.md](api-core-services.md) を参照する。

### 共通の形式

- 指示の本体は [instruction.schema.json](../../schemas/instruction.schema.json) に従う。`payload` は destination ごとの `schemas/dest/<name>.json` に従う。
- intake と gate の通常エラーは [api-error.schema.json](../../schemas/api-error.schema.json) の `{ "error": { "code", "message", "hint" } }` で返る。`hint` は実装によって空文字列の場合がある。
- API 例の `<...>` は利用者が置き換える値である。実在する host、token、メールアドレス、鍵は書かない。
- `request_id` は caller が作る再試行単位である。gate はこれを intake の `idempotency_key` に変換する。異なる本文で同じ key を再利用してはならない。
- `X-AX-Agent`、`X-AX-Model`、`X-AX-Session` は表示・監査用の申告であり、権限ではない。gate が受ける公開側の名前は、大文字・小文字を問わない `x-ax-agent`、`x-ax-model`、`x-ax-session` である。

### 受け口と身元

| via | 接続経路 | 身元を決める根拠 | `from` | 最大 tier |
| --- | --- | --- | --- | --- |
| `local` | commander の Unix socket | `SO_PEERCRED` の UID と `AX_UID` | `commander` | `operate` |
| `ssh` | seat の Unix socket | `SO_PEERCRED` の UID と有効な SSH seat | `commander` | seat の tier |
| `hq` | HQ 専用 Unix socket | `SO_PEERCRED` の UID と `AX_HQ_UID`。加えて verifier の証跡 | `hq` | `irreversible` |
| `tailnet` | gate から intake への relay | gate が照合した Tailnet peer、seat token、gate credential | `commander` | seat の tier |

`from`、`principal`、`via`、`issued_by`、`approved_via`、`tier`、`confirm_irreversible` は caller が権限を作るために使えない。受け口が確定した値と食い違えば `403 forged_authority` になる。

## intake

intake の主な API は Unix socket の HTTP/1.1 で受ける。`local`、`ssh`、`hq` の socket は接続時に `SO_PEERCRED` を読み、許可されない UID は HTTP header を読む前に `403 auth.failed` で閉じる。HTTP は `Content-Length` の本文だけを受け、chunked 本文は `501 not_implemented`、本文が 10 MiB 超なら `413 too_large` である。

`tailnet` は gate からの TCP relay を使う設計である。intake は relay を信頼する前に接続元アドレスと `X-AX-Gate-Token` を照合し、relay が渡す device 情報と bearer token を seat と照合する。ただし現在の登録には gate 用の待ち受けがなく、relay は接続できない。仮に待ち受けを設けても、gate の接続元とトークンが空のため認証で拒否される。caller が intake へ直接これらのヘッダーを付けても認証にはならない。

### `GET /healthz`

| 項目 | 内容 |
| --- | --- |
| 認証 | 不要。health 用 listener は loopback だけを想定し、指示は受けない。 |
| 要求 | 本文なし。 |
| 応答 | `{ "ok": true, "component": "intake", "version": "..." }` に health の補足状態を加える。 |
| エラー | listener が無い場合は接続できない。アプリ内の例外は `500 internal`。 |
| 例 | `curl http://<health-host>:<health-port>/healthz` |

### `POST /v1/instructions`

| 項目 | 内容 |
| --- | --- |
| 認証 | 受け口で確定した `Ident` が必須。`local`、`ssh`、`hq`、`tailnet` で許可範囲が異なる。 |
| 要求 | [instruction.schema.json](../../schemas/instruction.schema.json)。`destination` の `payload` は登録簿が指す `schemas/dest/<name>.json`。 |
| 応答 | 同期成功は `{ "ok": true, "instruction_id", "destination", "status", "result", "replayed" }`。非同期 destination は `202` と `status: "pending"`。承認待ちは `202` と `reason: "approval_required"`。 |
| エラー | `400 bad_json`、`400 idempotency_required`、`403 auth.failed`、`403 forged_authority`、`409 idempotency_mismatch`、`422 bad_request`、`501 contract_unresolved`、`503 handler_unavailable`、`503 conflict_unavailable`。destination 固有の拒否もこの形式で返る。`handler` が `not-implemented:` の行き先は、`payload` の検査より前に `501 contract_unresolved` を返す（実装済みの行き先は従来どおり `422 payload_schema` で拒否する）。 |
| 冪等性 | 同一 destination、同一 key、同一 payload は完了結果を `200` で再送する。処理中は `202` と `replayed: true`。同じ key で payload が異なれば `409`。 |

Unix socket 経由の例:

```sh
curl --unix-socket /run/commander/commander.sock \
  -H 'Content-Type: application/json' \
  --data '{
    "from":"commander",
    "destination":"<destination>",
    "payload":{},
    "idempotency_key":"req-example-001"
  }' \
  http://socket.invalid/v1/instructions
```

この例の `payload` は意図的に空である。実行する場合は destination のスキーマで必須の項目を渡す。例えば `seat.revoke` は [seat.revoke.json](../../schemas/dest/seat.revoke.json)、秘密の登録要求は [secret.push.json](../../schemas/dest/secret.push.json) を使う。秘密の値はこの API に渡さない。

### `GET /v1/destinations`

| 項目 | 内容 |
| --- | --- |
| 認証 | `Ident` が必須。via と tier で読める destination だけを返す。 |
| 要求 | 本文なし。 |
| 応答 | `{ "destinations": [{ "name", "title", "schema", "tier", "mcp_tool"?, "x-ax-unresolved"? }], "reads": [] }`。`schema` は登録簿上の schema 名である。`handler` が `not-implemented:` の行き先には、その理由を示す `x-ax-unresolved` が付く。この行き先への指示は payload の検査より前に `501 contract_unresolved` で拒否される。 |
| エラー | `403 auth.failed`。 |
| 例 | `curl --unix-socket /run/commander/commander.sock http://socket.invalid/v1/destinations` |

### 読み取り relay: `GET /v1/read/{op}`

実装済みの `op` と許可する query key は `QUERY_KEYS` に固定される。gate の許可集合とは異なるため、両者を混同しない。

| route | 許可される query | 応答 |
| --- | --- | --- |
| `GET /v1/read/status` | なし | `control` と `timeline` の状態を部品ごとに `{ "ok", "data" }` または `{ "ok": false, "error": "unavailable" }` で返す。 |
| `GET /v1/read/todos` | `state`、`project`、`subloop`、`due` | `ledger` の同じ部品別形式。 |
| `GET /v1/read/events` | `after`、`kind`、`subloop`、`project`、`subject`、`limit` | `timeline` の同じ部品別形式。 |

| 項目 | 内容 |
| --- | --- |
| 認証 | `from=commander` かつ `via` が `local`、`ssh`、`tailnet` のいずれか。tier が有効であること。HQ はこの route を使わない。 |
| エラー | 未許可の受け口または tier は `403`、未知の `op` は `404`、GET 以外は `405` と `Allow: GET`。query の余分な key は捨てられ、下流へ渡らない。 |
| 例 | `curl --unix-socket /run/commander/commander.sock 'http://socket.invalid/v1/read/todos?state=open&project=<project>'` |

### 承認 API

承認の登録が有効な場合だけ追加される route である。HQ の Unix socket と、未確定ではない `hq_verifier` の両方が必要である。

| route | 認証と要求 | 応答とエラー |
| --- | --- | --- |
| `GET /v1/approvals` | `via=hq`。本文なし。 | `{ "approvals": [...] }`。verifier が無ければ `501 approval_binding_missing`。 |
| `GET /v1/approvals/{approval_id}` | `via=hq`。本文なし。 | 承認の保存内容。存在しない ID などは approval service のエラー。 |
| `POST /v1/approvals/{approval_id}/grant` | `via=hq`。本文は `{ "digest": "<64-hex>" }`。証跡には本人性と second factor が必要。 | 承認処理の結果。本文不正は `422 bad_request`、未認証・不適格は `403`、verifier 未接続は `501 approval_binding_missing`。 |

`approval.grant` と `approval.deny` を指示として送る場合の payload は、それぞれ [approval.grant.json](../../schemas/dest/approval.grant.json)、[approval.deny.json](../../schemas/dest/approval.deny.json) に従う。HQ は利用者が確認した digest を再利用する。caller が `second_factor: true` のような JSON を書いても証跡にはならない。却下（`approval.deny`）も承認と同じく second factor を要る。第二要素の証跡が無い却下は `403 bad_code` で拒否され、承認待ちの状態は変わらない。取り下げ（`approval.withdraw`、[approval.withdraw.json](../../schemas/dest/approval.withdraw.json)）は second factor を要らない。

### seat の登録 API

| route | 認証と要求 | 応答とエラー |
| --- | --- | --- |
| `POST /v1/seat/enroll` | `via=tailnet` だけ。bearer seat token は不要（他の seat ルートは bearer が無いと fail closed する）。本文は `{ "code": "<32-lowercase-hex>" }`。gate が確認した device 情報で最初の引換時に device を結び付ける。 | `200` で `{ "seat_id", "token", "expires_at" }`。token はこの一度だけ返す。`400 bad_request`、`401 unauthorized`、`503 locked` があり得る。 |
| `POST /v1/seat/session` | SSH seat 用に予約されている。 | 現在は `501`。session の JSON wire は未決定。 |

seat を作る前段の payload は [seat.enroll_start.json](../../schemas/dest/seat.enroll_start.json) または [seat.add.json](../../schemas/dest/seat.add.json) を使う。後者は SSH 公開鍵を含むため、HQ の本人確認と second factor を通る。

## gate

gate は Tailnet peer の送信元 IP、peer 記録、許可ユーザー、bearer の digest、seat の状態と有効期限を検査する。HTTP の `Host` は 1 個だけで、設定された host と port に一致しなければ `421 host_not_allowed` である。`Origin` は拒否する。GET と POST 以外は `405 method_not_allowed` である。

`/healthz` を除く route は、特記しない限り bearer seat token が必要である。gate は応答から token、公開鍵、登録コード、内部 credential に当たる名前を取り除く。ただし enrollment 応答だけは token を一度返せる。

### `GET /healthz`

| 項目 | 内容 |
| --- | --- |
| 認証 | 不要。ただし有効な `Host` が必要。 |
| 応答 | `{ "ok": true }`。 |
| エラー | `421 host_not_allowed`、`403 origin_not_allowed`、`405 method_not_allowed`。 |
| 例 | `curl -H 'Host: host-a.example.invalid:8909' http://host-a.example.invalid:8909/healthz` |

### `POST /v1/enroll`

| 項目 | 内容 |
| --- | --- |
| 認証 | bearer は不要。Tailnet peer と許可ユーザーの照合は必要。登録失敗は device 単位で制限する。 |
| 要求 | `{ "code": "<32-lowercase-hex>" }` だけ。余分な key、別形式、非文字列は `400 bad_request`。 |
| 応答 | intake の `POST /v1/seat/enroll` の `{ "seat_id", "token", "expires_at" }`。この route だけが token を返す。 |
| エラー | `401 peer_unknown`、`400 bad_request`、`429 rate_limited`、`503 locked`、`503 upstream_unavailable`。 |
| 例 | `curl -X POST -H 'Host: host-a.example.invalid:8909' -H 'Content-Type: application/json' --data '{"code":"<enrollment-code>"}' http://host-a.example.invalid:8909/v1/enroll` |

### `GET /v1/*` と `POST /v1/instructions`

| route | 認証と要求 | 応答とエラー |
| --- | --- | --- |
| `GET /v1/status`、`/v1/todos`、`/v1/events` | bearer seat token。query は intake の `QUERY_KEYS` を使う。 | intake の読み取り relay 応答。下流が利用不能なら読み取りは `200` と `intake.ok: false` を返す。 |
| `GET /v1/whoami` | bearer seat token。gate 内部では `/v1/read/whoami` に変換する。 | 現在の intake `QUERY_KEYS` に `whoami` は無いため、追加の read route が無ければ利用不能である。 |
| `GET /v1/destinations` | bearer seat token。gate 内部では `/v1/read/destinations` に変換し、seat tier で再度フィルターする。 | 現在の intake `QUERY_KEYS` に `destinations` は無い。追加の read route が無ければ利用不能である。直接の intake route は `/v1/destinations`。 |
| `GET /v1/read/{op}` | gate の許可集合 `READS` は `status`、`todos`、`events`、`brief`、`whoami`、`destinations`、`show`、`approvals`、`seats`、`decisions`、`guide`。 | 実際に handler があるかは intake extension に依存する。`wait` は常に `400 bad_request`。`approvals` だけは ID と `wait_s` を許す予定で、値は上限に丸める。 |
| `POST /v1/instructions` | bearer seat token。要求は gate 用に `request_id` を必須とする。`from`、`principal`、`via`、`issued_by`、`approved_via`、`approval_id`、`confirm_irreversible=true` は禁止。 | gate が `request_id` を `idempotency_key` に変換して intake へ渡す。`request_id` と既存 `idempotency_key` が異なれば `409 idempotency_conflict`。tier 不足は `403 tier_insufficient`。 |

書き込みの例:

```sh
curl -X POST \
  -H 'Host: host-a.example.invalid:8909' \
  -H 'Authorization: Bearer <seat-token>' \
  -H 'Content-Type: application/json' \
  -H 'X-AX-Agent: <agent>' \
  --data '{
    "request_id":"req-example-002",
    "destination":"<destination>",
    "payload":{}
  }' \
  http://host-a.example.invalid:8909/v1/instructions
```

gate は read を毎秒、write を毎分、seat ごとの同時実行数で制限する。超過時は `429 rate_limited` と `Retry-After` を返す。本文上限を超えれば `413 too_large`、JSON object でなければ `400 bad_json`、seat token が無ければ `401 unauthorized` である。

### `POST /mcp` と `POST /mcp/`

| 項目 | 内容 |
| --- | --- |
| 認証 | `GET /v1/*` と同じ seat token。`initialize` や `tools/list` も intake の destination catalog を読む。 |
| 要求 | JSON-RPC 2.0。実装する method は `initialize`、`tools/list`、`tools/call`、通知の `notifications/initialized`。 |
| 応答 | 正常時は JSON-RPC result。tool の本文は `result.content[0].text` に JSON 文字列として入る。通知は `202`、本文なし。 |
| エラー | JSON-RPC の `-32600`、`-32601`、`-32602`。認証前の gate エラーは通常の API error 形式。tool 名不明は result 内の `tool_not_found`。 |
| 例 | `curl -X POST -H 'Host: host-a.example.invalid:8909' -H 'Authorization: Bearer <seat-token>' -H 'Content-Type: application/json' --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}' http://host-a.example.invalid:8909/mcp` |

### gate から intake への relay

**Status: provisional.** 次の wire は `HttpRelay` と `decode_relay_evidence` による実装である。仕様上の正式な credential wire として固定済みではない。

gate は JSON 本文を 1 接続 1 要求で relay し、下の検査済みの値だけを渡す。下流の応答は 2 MiB まで、重複 response header は拒否する。

| relay header | gate が値を得る場所 | intake の扱い |
| --- | --- | --- |
| `X-AX-Gate-Token` | gate 専用 core token | 接続元アドレスと照合する。 |
| `X-AX-Device-Id`、`X-AX-Device` | 検証済み Tailnet peer | seat と enrollment の device 結び付けに使う。 |
| `Authorization: Bearer <seat-token>` | caller の bearer | seat の token digest と device ID を照合する。enrollment（`POST /v1/seat/enroll`）では不要で、無くても通る。それ以外の relay 要求では必須で、無ければ 401 になる。 |
| `X-AX-Request-Id` | gate が作る ULID | relay の相関用。 |
| `X-AX-Agent`、`X-AX-Model`、`X-AX-Session` | caller の短い申告 | 権限ではない。改行を含む値は拒否する。 |

## HQ

HQ は Unix socket runtime で動く。socket directory は所有者、`0700`、symlink でないことを確認し、socket 自体は `0600` で作る。入力画面から intake へ直接 TCP 接続する実装は無く、`AX_HQ_INTAKE_SOCKET` または既定の Unix socket だけを使う。

### HQ の認証境界

`/healthz` 以外はすべて認証する。Tailnet 側は `X-Forwarded-For` と `Tailscale-User-Login` を 1 個ずつ要求し、許可ユーザー一覧と照合する。公開 proxy 側は `CF-Access-Jwt-Assertion` を issuer、audience、JWKS、期限で検証する。公開経路から次を要求すると private route は `404 not_found`、秘密入力は `403 secret_local_only` になる。

- `/seats`、`/approval/*`、`/approvals/*`
- `/v1/hq/approval*`、`/v1/hq/seats*`、`/v1/hq/b12b2/*`
- `/v1/secrets`、`/api/secrets/*`

書き込みでは、設定された origin だけを受け、cross-site の `Sec-Fetch-Site` は拒否する。sample mode では GET、HEAD、OPTIONS 以外が `409 sample_read_only` になる。

### 秘密入力

| route | 認証と要求 | 応答とエラー |
| --- | --- | --- |
| `GET /v1/secrets` | 認証済みのローカル利用者。本文なし。 | `{ "slots": [...] }`。値ではなく slot の metadata だけを返す。store 無しは `503 slots_unavailable`。 |
| `POST /v1/secrets/{slot}` | 認証済みのローカル利用者。`Content-Type: application/json`。本文は `value` 必須、任意で `operation` と `request_id`。400,000 bytes 以下。 | slot store の結果。`415 json_required`、`400 bad_request`、`413 too_large`、slot 側の `SecretFailure`、`503 secret_store_failed`。 |

例:

```sh
curl --unix-socket /run/hq/hq.sock \
  -X POST -H 'Content-Type: application/json' \
  --data '{"value":"<secret-value>","operation":"write","request_id":"secret-example-001"}' \
  http://socket.invalid/v1/secrets/<slot>
```

秘密の値を `POST /v1/instructions`、gate、approval URL、ログ、画面完了メッセージへ入れてはならない。秘密を使う側に送る指示は [secret.push.json](../../schemas/dest/secret.push.json) の slot 名だけである。

### 承認ページと second factor

次の route は HTML、CSS、JavaScript、または HTML の結果画面を返す。POST の body は JSON または `application/x-www-form-urlencoded` であり、64 KiB 以下である。`page_id`、`csrf_token`、`code` は発行済みページを消費するための共通フィールドである。

| route | 要求 | 成功時 | 主なエラー |
| --- | --- | --- | --- |
| `GET /approval/settings` | 認証済み利用者。 | factor の設定画面。 | `403`、`503 approval_unavailable`。 |
| `POST /v1/hq/approval/enroll` | 共通フィールドだけ。 | factor 登録用の HTML。 | `422 bad_request`、factor service のエラー。 |
| `GET /approvals/{approval_id}` | 認証済み利用者。 | digest を表示する承認画面。 | `501 hq_approval_binding_missing`、`503 approval_unavailable`。 |
| `POST /v1/hq/approvals/{approval_id}/grant` | 共通フィールド。second factor を検証する。 | 承認結果 HTML。 | `403`、`422 bad_request`、`501 hq_approval_binding_missing`。 |
| `POST /v1/hq/approvals/{approval_id}/deny` | 共通フィールド。second factor を検証する。 | 却下結果 HTML。 | `403`、`422 bad_request`、`501 hq_approval_binding_missing`。 |
| `GET /v1/hq/b12b2/ui.js`、`GET /v1/hq/b12b2/ui.css` | 認証済み利用者。 | UI asset。 | 認証エラー。 |

### seat と enrollment の画面

| route | 要求 | 成功時 | 主なエラー |
| --- | --- | --- | --- |
| `GET /seats` | 認証済みの直接 Tailnet 利用者。 | seat と pending approval の HTML。 | verifier や intake が未接続なら `501` または `503`。 |
| `POST /v1/hq/seats/prepare` | 共通フィールドと `destination`。`payload` を渡す場合はその destination の schema に従う。個別 field を渡す場合は `seat.enroll_start`、`seat.tier`、`seat.revoke`、`seat.lock`、`seat.unlock` の UI 用の field を使う。 | 確認画面 HTML。 | `422 bad_request`、未解決 schema は `501 hq_approval_binding_missing`。 |
| `POST /v1/hq/seats/apply` | 共通フィールド。前段で保存した確認内容を使う。`seat.add`、`seat.enroll_start`、`seat.tier`、`seat.unlock` は second factor が必須。 | 完了画面 HTML。enrollment code や鍵 fingerprint は trusted response adapter が提供できるときだけ表示する。 | `403 second_factor_required`、`409 approval_mismatch`、`502 registration_bad_response`、`503 approval_unavailable`。 |

`payload` を使う場合の schema は [seat.add.json](../../schemas/dest/seat.add.json)、[seat.enroll_start.json](../../schemas/dest/seat.enroll_start.json)、[seat.tier.json](../../schemas/dest/seat.tier.json)、[seat.revoke.json](../../schemas/dest/seat.revoke.json)、[seat.lock.json](../../schemas/dest/seat.lock.json)、[seat.unlock.json](../../schemas/dest/seat.unlock.json) である。

## 未確定事項と HEAD との差

### gate-to-intake credential wire

**Status: provisional.** 作業ツリーは `HttpRelay`、`wire_headers`、`decode_relay_evidence` を追加し、gate の実行時 relay を有効にしている。HEAD の relay は未解決 transport で、実通信をしない。上の header 名、JSON の符号化、downstream host/port の扱いは現行コードを説明しており、正式な仕様が決まるまで互換約束ではない。

### enrollment と seat.add

**Status: working-tree behavior.** `POST /v1/enroll` と intake の `POST /v1/seat/enroll` は、32 桁の小文字 16 進コードを受け、最初に成功した gate relay の device ID へ結び付ける。HEAD では enrollment wire が未決定で `501` だった。作業ツリーは `seat.add` の schema と SSH seat 作成も追加している。HQ の登録結果を表示する trusted response adapter はまだ別途必要である。

### HQ verification-result wire

**Status: provisional.** HQ の `UnixIntake.proof_encoder` と intake の `hq_verifier` は、同じ検証済み証跡を受け渡すために注入する契約である。既定値は無く、caller の boolean や JSON field を証跡として採用しない。いずれかが未接続なら、承認・HQ 経由の irreversible 操作・seat 操作は `501 approval_binding_missing` または `501 hq_approval_binding_missing` で fail closed する。

### 読み取り route の差

**Status: current implementation.** gate の `READS` は 11 種類だが、intake の `QUERY_KEYS` が route を実装するのは `status`、`todos`、`events` の 3 種類だけである。`/v1/whoami` と `/v1/destinations` は gate が internal read route へ変換するため、追加 extension が無い環境では利用できない。gate の catalog 用 read route を整備する前に、この差を前提に client を実装してはならない。

## 設定値と環境変数

値はここに記録しない。表は API 境界に関係する変数名、用途、コード上の既定の有無を示す。

| 変数 | 用途 |
| --- | --- |
| `AX_UID` | intake `local` socket の `SO_PEERCRED` UID。未指定時は intake の実効 UID。 |
| `AX_LOCAL_PRINCIPAL` | `local` の表示 principal。未指定時は実装の既定値。 |
| `AX_HQ_UID` | HQ socket listener と `hq` の `SO_PEERCRED` UID。未指定なら HQ listener を追加しない。 |
| `AX_HEALTH_PORT` | intake health listener の loopback port。未指定時は `8080`。 |
| `AX_LOG_LEVEL` | intake 起動時の log level。 |
| `AX_HOST`、`AX_BIND_ADDR` | gate の必須 host と bind address。これらと gate port が受け入れる `Host` を決める。 |
| `AX_GATE_PORT` | gate port。未指定時は `8909`。 |
| `AX_GATE_STATE_DIR`、`AX_SEATS_FILE` | gate が peer と seat の読取状態を検査する場所。 |
| `AX_GATE_CORE_TOKEN_FILE` | gate と intake の relay credential を読む場所。値を HTTP client や文書に書かない。 |
| `AX_GATE_ALLOWED_USERS_FILE` | gate が Tailnet peer の user を照合する一覧。 |
| `AX_GATE_INTAKE_HOST`、`AX_GATE_INTAKE_PORT` | 作業ツリーの `HttpRelay` が接続する downstream。port の既定値は `8082`。 |
| `AX_CONFIG_DIR` | gate、HQ が設定ファイルを読む基点。 |
| `AX_HQ_ACCESS_CONFIG` | HQ access 設定の YAML。未指定時は `AX_CONFIG_DIR` 配下の既定名。 |
| `AX_PUBLIC_PROXY_IP` | HQ が public proxy と Tailnet を分類するためのアドレス。 |
| `AX_HQ_INTAKE_SOCKET` | HQ から intake へ送る Unix socket。未指定時は実装の既定 socket。 |
| `AX_REDACT_INDEX`、`AX_PII_LIST` | HQ business API が redactor を設定するための組。両方がある場合だけ読む。 |

実装確認の対象は `core/intake/httpsock.py`、`core/intake/vias.py`、`core/intake/app.py`、`core/intake/seat/`、`core/gate/`、`core/hq/`、および `schemas/` である。外部 host への接続、実在 token の使用、実サービスへの curl は行っていない。

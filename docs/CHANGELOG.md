# 実装者の判断の記録

S-1.3 に従い、文書に無い細部を決めたときの記録。1件ごとに、日付、何を決めたか、理由、埋めた節。追記だけで、書き換えない。

## Unreleased

- **Model resources and worker execution:** Added a fresh model registry and persistent, atomic model and family slot reservations. Worker launches reserve capacity before starting, and queued jobs can be cancelled. Uncertain outcomes retain their reservations until reconciled.
- **Work control:** Connected authenticated entry points for work control and recorded cancellation requests. Worker results are checked before success is reported.
- **Environment jobs and remote audits:** Added authenticated environment job submission and status APIs, persistent job state, and reconciliation of uncertain results. A restricted SSH transport supports read-only audits and artifact retrieval.
- **Sandbox execution:** Added policy checks, admission controls, persistent gate state, and routes for running jobs in existing Instabox sandboxes. Timed-out or cancelled processes are stopped, while uncertain remote state remains recorded.
- **Conversation indexing:** Added guarded embedding and similarity search through registered model endpoints, with read-only conversation inputs and isolation when configuration is missing or invalid.
- **Identity and approvals:** Added signed, short-lived identity proofs for privileged actions, replay protection, and approval service integration. Routine operations remain available when proof signing is unavailable.
- **Publication safeguards:** Added release scanning for secrets, personal information, internal references, and supported image location metadata. Missing required inputs or unavailable publication dependencies keep the release gate closed.
- **Runtime images:** Pinned several runtime base images by digest; the core and CI Dockerfiles still use mutable tags by default unless overridden.

### Known limitations

- Production second-factor notifications are not connected. Live identity-provider integration and notification delivery have not been verified.
- Actual publication remains disabled. Publication credentials, delivery operations, and required production configuration are not fully connected or verified.
- Instabox supports existing sandboxes only; creating and deleting sandboxes is not supported.
- Remote execution currently supports the read-only audit entry point; other environment entry points remain unsupported. Production credentials and host verification configuration still need deployment and verification.
- Conversation indexing requires an explicitly configured history source, credential, and registered embedding endpoint. Image scanning does not include image recognition or inspection of compressed image content.
- Tests used simulated dependencies for external operations. Deployment, real machines, live SSH, live notifications, and actual publication have not been verified.

### Pending integration

- **Subloop creation** — TODO: Confirm after integration. Production approval, ledger, and candidate-branch connections are incomplete.
- **Security watch** — TODO: Confirm after integration. The timer and notification request path are connected; live provisioning, SSH, and notification delivery remain unverified.
- **Change operations** — TODO: Confirm after integration. Trusted pull-request identity and the shared ledger are required before enabling merge cancellation.
- **Shared sandbox capacity arbitration** — TODO: Confirm after integration. Sandbox and environment jobs do not yet share an atomic capacity reservation.
- **Publication review** — TODO: Confirm after integration. Review integration and its production dependencies require final verification; actual publication remains disabled.

## 2026-11-07 B0（b0）

- 2026-11-07。ULID は標準ライブラリで実装した（`axcommon/ids.py`）。理由: F-14.1 の依存に python-ulid があるが、ホストのスクリプト（`python3` と PyYAML だけ）と CI の素のイメージでも `axcommon` の基本部が動くようにするため。形式は Crockford の base32（48 ビットの時刻 + 80 ビットの乱数）で python-ulid と同じ。埋めた節: S-2.1、D-1。
- 2026-11-07。`axcommon/profile.py` を作り、profile から compose の変数への対応（F-18.6 の表）を Python にまとめた。`host/ax-env.sh` と `host/ax-up.sh`、`host/ax-redact-index.sh` はこれを呼ぶ。理由: 表の変換（`~` の展開、`dirname`、相対パスの検査）を bash で書くと試験しにくい。埋めた節: F-18.6。
- 2026-11-07。`ax-env.sh` が失敗にする「必須の項目」は、F-18.6 の表の項目のうち `public_proxy_ip` を除く 13 項目とした。理由: 表が必須を示していない。`public_proxy_ip` は B10 の `hq` が使う値で、B0〜B7 の環境では無いことがある。埋めた節: F-18.6。
- 2026-11-07。compose の変数 `AX_GATE_PORT` を `ax-env.sh` が書き、`tools/ci-fixtures/ci.env` にも入れた。理由: D-14 と S-15.5 は `gate` の ports を `${bind_addr}:${gate_port}` と書くが、F-18.6 の表に対応する変数が無い。表への追記は文書の側の作業で、OPEN.md に残した。埋めた節: F-18.6、D-14、S-16.3。
- 2026-11-07。compose のサービスの共通設定（`TMPDIR=/tmp-hdd`、`/tmp-hdd` の bind、healthcheck、ログの大きさ、`cap_drop`、`no-new-privileges`）は `docker/base.yaml` の `extends` で各サービスが受ける。理由: YAML のアンカーはファイルをまたげず、サービスは `compose.d/<束の ID>.yaml` に分かれるため。`extends.file` は project directory（`${AX_HOME}`）からの相対で書く（Compose 2.40.3 で確かめた）。埋めた節: D-14、F-18.1。
- 2026-11-07。tools/ci.sh の compose 検査は、F-14.1 の `config --format json` に `--profile '*'` を足した。理由: プロファイルで止めてあるサービス（B0 の `axcommon-selfcheck`）も `ports` などの検査に入れるため。埋めた節: F-14.1。
- 2026-11-07。tools/compose_check.py に F-14.1 の (c)〜(e) のほか、(f) `gate` 以外のサービスに `ports` が無いこと、(g) `gate` が `ax-core` だけに入ること、(h) 網の ipam・bridge 名・internal を足した。理由: D-14 の「検査に、`gate` が `ax-core` だけに入ることと、`gate` 以外の `ports` が増えていないことを足す」と F-18.1 の網の表。埋めた節: D-14、F-18.1。
- 2026-11-07。`tools/ci.sh` は `tools/ax_graph_check.py` が在れば `plugins/subloop` と `templates` を引数にして呼ぶ。引数の形は B7 が決める文書上の約束が無いため、この形にした。B7 が違う形にするときは `tools/ci.sh` の (2) を直す。埋めた節: F-14.1、D-6.4。
- 2026-11-07。制御 API の発行者別トークンの秘密の名前を `control-token-manager`、`control-token-backup`、`control-token-inspection`、`control-token-emergency` とし、ホストの置き場を `${secrets_dir}/control-tokens/<名前>` とした。理由: S-5.3 は「専用の資格（compose の secrets に置く名前付きトークン）」と書くだけで、名前が無い。埋めた節: S-5.3、D-14。
- 2026-11-07。outbox の形式: ボリューム内の `outbox/pending.jsonl`（1 行 1 JSON。`schemas/outbox-entry.schema.json`）。送れた物は `outbox/sent/<日付>.jsonl`、受け取り側が恒久的に拒否した物（4xx）は `outbox/rejected/<日付>.jsonl` へ移す（削除しない。F-1）。理由: D-2.2 は「`outbox/` に溜める」とだけ書く。埋めた節: S-2.2、D-2.2。
- 2026-11-07。`axcommon.client` は標準ライブラリ（urllib）で書き、転送を差し替えられる形にした。理由: 試験でソケットを使わないため（偽の転送 `tests/fakes/http.py`）。httpx は S-25 の箱の部品（別の束）が使える。埋めた節: D-2.2、D-15（B0）。
- 2026-11-07。`config/defaults.yaml` は SPEC.md の S-17 の表から `docs/b0/gen_defaults.py` で生成した。表に名前のない「再試行の待ち」は `retry_backoff`（base_s 30、factor 2、max_s 1800）とした。埋めた節: S-17。
- 2026-11-07。`config/error-rules.yaml` の規則の形（`match` の鍵 http_status、http_status_range、body_regex、exception、exit_code、hint）と評価の順は実装者が決めた。S-2.3 の表の「判定の手がかり」を機械で読める形にした。`artifact.bug` の 4 つの code は、出した場所が `hint` で指す。埋めた節: S-2.3。
- 2026-11-07。`axcommon.policy.allowed()` の規則は S-15.1 の表のとおりとし、`tier` の順は free < flash < standard < last_resort（S-8.2、S-8.4）。埋めた節: S-15.1、D-14。
- 2026-11-07。トークンの表 `TokenTable` は 1 行 `<名前> <sha256 の 16 進>`（`core-tokens.sha256` の形）で読む。トークンのファイルは末尾に改行を付けず、sha256 は改行と空白を除いた文字列に対して取る。埋めた節: F-18.4。
- 2026-11-07。`axcommon.ext.load_extensions()` は、拡張 1 件の読み込みや `register` の失敗でその部品を止めず（fail-soft）、失敗した拡張の名前を `/healthz` の `ext_failed` に出す。埋めた節: PLAN.json の conventions。
- 2026-11-07。`docs/` に INDEX.md と CONSISTENCY.md の写しも置いた。理由: `tools/trace_check.py` と `tools/build_index.py` が INDEX.md を読む。使い方は `python3 tools/trace_check.py docs`。埋めた節: INDEX.md の(a)。

## 2026-11-07 B7（node-dirs）

- 2026-11-07。サブループ固有のノード型の宣言的な置き場 `node_dirs` を足した。`nodes.yaml`（サブループのディレクトリ、またはその直下の1階層）に `node_dirs:` を相対パスの list で書くと、その置き場が共通の部品庫と `<id>/nodes/` の後に読まれる。理由: S-7.4 は型を足す場所を `plugins/nodes/` に限るが、担当パスの制約で `nodes/` を置けないサブループ（b14-4 の `integration/**`）があり、OPEN.md の[統合で対応]の項で宣言的対応が要望されていた。manifest には置かない（schemas/manifest.schema.json は additionalProperties: false の共有ファイルで、b0 の担当外のため）。宣言の読み込みに失敗した `nodes.yaml` はその宣言だけを飛ばす（fail-soft）。読む順の規則は `load_registry` と同じ（先の物が残り、後の同名は duplicate）。埋めた節: S-7.4、D-6.3、OPEN.md 50 行目。
- 2026-11-07。`check_graph` の未使用の引数 `node_dirs`（ea8b618 で署名だけ入っていた）を削除した。理由: サブループ固有の型は登録簿（registry）の側に足す形に統一し、使われない引数を残さない。呼び出し側（check_subloop_dir、__main__、ax_graph_check）は `declared_node_dirs` / `subloop_registry` / `load_registry_into` を使う。埋めた節: D-6.4。

## 2026-11-08 B16 旧候補 ID の確認

- hub-host の旧 X キューを読み取りで確認した。queue・posted・held の直下にはそれぞれ 1・10・3 件あり、すべて `YYYYMMDD-HHMM-` で始まる名前だった。`ax-x-` で始まる名前はなかった。AX の候補 ID は `ax-x-<ULID>` で始まる。確認は名前の形式に限り、旧データは変更していない。対象: S-23.2、D-21.2、T-155。

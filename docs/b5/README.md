# B5 入口と行き先の登録簿（intake の骨格）

対応する段: B5。読んだ節: S-6.1〜S-6.6、D-5、F-9.2、F-18.4。受け持つ試験: T-6（入口の骨格の部分。
T-6 の残り（3 つの受け口の同等性、作業側の網の偽コンテナの到達不能）は B12 以降）。

## できること

- 指示を受け、検証して、行き先へ渡す（S-6.1、S-6.5）。
  - 登録のない行き先、`allowed_from` 外、`via` 外、Schema 違反、期限切れ、段不足、冪等キー不足を拒否する（`instruction.rejected`）。
  - 本文の `from` が資格から決めた値と違う、`confirm_irreversible` の偽装、payload の中の資格の名前を拒否する（`forged_authority`）。
  - 内部コンテナは受け口に届かない（compose.d/b5.yaml で intake は ax-core だけ。ports 無し）。commander ソケットは接続元 uid の照合（`SO_PEERCRED`）を通らないと 403（`auth.failed`）。
- 行き先の登録簿（S-6.3）: `config/intake-destinations.yaml` と `config/intake-destinations.d/*.yaml`。
  起動時とファイルの更新時刻が変わったときに読み直す。壊れた読み直しでは前の登録簿を使い続け、`health.alert` を書く。
  行き先 1 行を足すだけで `GET /v1/destinations` と `POST /v1/instructions` に反映される（再起動なし）。
- 依頼 ID（S-6.16 の核）: `(destination, idempotency_key)` の照合、同じ組の再送には前の結果を返す（`replayed: true`）、
  違う本文なら 409（`idempotency_mismatch`）。置き場は `/state/intake/idempotency.jsonl`。
- commander ソケット（F-9.2）: `/run/commander/commander.sock`。起動のたびに古いソケットを外し、`umask 077` で
  bind し、権限 0600。ディレクトリの所有者・権限が条件を外せば待ち受けず `health.alert`。
  受けた要求は `${ax_state_dir}/commander/access.log` に 1 行ずつ残す。
- 最初の行き先 2 つ: `control.command`（issued_by を受け口で決めて制御 API の `POST /v1/commands` へ）、
  `ledger.answer`（`answer_source: relay` と `quote` を添えて台帳の `POST /v1/todos/{todo_id}/answer` へ）。

## 構成

| 場所 | 役割 |
|---|---|
| `core/intake/app.py` | アプリ。`POST /v1/instructions`、`GET /v1/destinations`。healthz 用の別アプリも作る |
| `core/intake/registry.py` | 登録簿の読み込み・検査・読み直し、conflict_key の書式 |
| `core/intake/validation.py` | S-6.5 の検証。拒否の reason は文書の物を使う |
| `core/intake/vias.py` | 受け口の身元。`local`（uid 照合）と `hq` の骨格 |
| `core/intake/idempotency.py` | 依頼 ID の照合（S-6.16 の核） |
| `core/intake/httpsock.py` | Unix ソケットと TCP の小さな HTTP サーバ（ASGI。uvicorn 不使用） |
| `core/intake/audit.py` | `access.log` への追記 |
| `core/intake/dest/b5/` | `control.command` と `ledger.answer` のハンドラ |
| `core/intake/ext/` | 後続の束の拡張（`register(app)`）の置き場 |
| `compose.d/b5.yaml` | intake のサービス（ax-core だけ、ports 無し、`user: ${AX_UID}:${AX_GID}`） |

## 後続の束の差し込み口

- 行き先の追加: `config/intake-destinations.d/<pkg>.yaml`（handler が http(s) の URL ならコード不要）。
  特別な処理が要るときは `core/intake/dest/<pkg>/__init__.py` の `register(app)` でハンドラを登録する。
- アプリへの拡張: `core/intake/ext/<pkg>.py` の `register(app)`（axcommon.ext が読む）。
- 衝突の検出（S-6.17）: `app.state.intake.conflict_guard`（b12b3）。
- 承認（S-6.10）: `app.state.intake.approval_check`（b12b2）。
- 追加の待ち受け（seat.sock、hq ソケット、gate 用の TCP）: `app.state.intake.extra_listener_factories`（b12b1）。
  `core/intake/vias.py` の `register_via_factory` で受け口の身元の判定を足す。

## 決めた事と決まらない事

決めた内部の細部は `build/DECISIONS.md`、文書で決められない物は `build/OPEN.md`、
試験の表は `build/status/b5.md` を見る。

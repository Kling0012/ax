# B1/B2 時系列記録・退避コネクタ・記憶コネクタ

担当パッケージ: b1。対応する段: B1、B2。試験: T-1、T-117、T-2、T-3。

## 部品

| 部品 | 場所 | 口 | 役目 |
|---|---|---|---|
| timeline | `core/timeline/` | 8080（ax-core、全 API）、8081（ax-work、許可した API だけ） | 追記だけの事象の記録。投影 5 つ。書き出し |
| memory-connector | `core/memory-connector/` | 8080、8081 | agent-memory への唯一の窓口。必須項目と秘密の検査 |
| archiver | `core/archiver/` | healthz のみ（ax-core、固定アドレス `192.0.2.12`） | 古い物を archive-host へ rsync し、内容を照合する |
| archive-init | `compose.d/b1.yaml` | なし | 各ボリュームの `archive/` を作る。一回だけ動く |
| secret 検査 | `axcommon/redact.py` | なし | S-15.3 の基準。timeline、memory-connector、ほかの束が使う |
| reap | `host/ax-archive-reap.sh` | なし | 照合済みの元を取り除く（ホスト側。印 `reap-enabled` がある間だけ） |

## timeline

- 追記専用。更新と削除の経路は、HTTP にも SQL にも無い。DB のトリガーも更新と削除を拒否する。
- `POST /v1/events` は 1 件か配列。同じ `id` は無視して成功を返す。応答は `schemas/ext/b1/events-post-response.schema.json`。
- `GET /v1/events?after=&kind=&subloop=&project=&subject=&limit=&wait=` の応答は `{events, next_after}`（`events-list-response.schema.json`）。`kind` はカンマ区切りで複数。`wait` は `longpoll_s` で打ち切る。
- `GET /v1/projections/{name}`: `plugins`、`runs`、`jobs`、`controls`、`health`。ほかは 404。`active=true` は `runs`、`jobs`、`controls` だけ。
- `POST /v1/export`: 範囲は `before_ts`（排他）、`to_seq`（含む）、`days`。省くと `timeline_hot_days` より古い区間。seq の 1 から連続した区間を、日付が変わるごとに `archive/timeline/timeline-YYYY-MM-DD-<先頭 seq>.jsonl` へ書く。索引は `archive/timeline/index.json`。自動の書き出しは 1 時間ごと。
- 事象の本文（`data`）は書き込み時に `axcommon.redact` を通す。当たった値は `[REDACTED]` になる。
- 投影は追記と同じトランザクションの中で、`seq` の順に更新する。必須の項目が無い事象は追記だけ成功し、`/healthz` の `projection_skipped` が増える。
- kind ごとの `data` の形では断らない。形（S-3.2）だけを検査する。S-3.6 と T-117 が、必須の項目が無い事象の追記を成功とするため。
- 8081 の規則: 資格は Bearer のプラグイントークン。`source.component` はプラグイン ID と同じでなければ 403。`kind` は `core/timeline/kinds.py` の一覧か manifest が宣言した物だけ。`principal`、`session`、`via` は本文から受けない。拒否は `events.rejected` に残る。
- 作り直し: `python -m core.timeline.admin rebuild-projections [--db …] [--archive …] [--verify]`。ホストの中で、`docker compose exec timeline` から使う。`--verify` は作り直す前の表と全行の `value`、`upto_seq` を比べる。

## memory-connector

- `POST /v1/remember`: `{project, subloop?, host, source, kind, content, supersedes?, valid_from?, refs?}`（`remember-request.schema.json`）。
- 必須項目が無ければ 422 `missing_fields`。秘密か個人情報に当たれば 422 `secret_detected`（種別と件数だけを返す。置き換えて送らない）。
- 保存の前に `memory_search` で重複を見る。重複なら保存せず、見つけた ID を `memory.distilled` に書く。`supersedes` があれば `memory_supersede` を使う。
- 接続できない（要求が届いていない）ときは `outbox/` に溜めて 202 `queued`。要求を送った後の切断・時間切れは再送せず、202 `unknown`。
- `POST /v1/search`、`GET /v1/memories/{id}`、`GET /v1/memories` は agent-memory の `memory_search`、`memory_get`、`memory_list` の窓口。
- 保存の範囲は共通記憶一つ（`visibility=project`、scope は `user:operator`、`project:all`、`agent:null`）。

## archiver

- 退避元は `/profile/archive-sources.yaml` の一覧と、各ボリュームの `archive/`（`/vol/<名>`、名前は `vol-<名>`）。
- 転送: `rsync -a --checksum`（`--delete` なし）。照合は同じ引数に `--dry-run --itemize-changes` を付けた再実行で、差分 0 件を合格とする。manifest も同じ場所へ送る。
- 失敗（接続不可、鍵の拒否、不一致、空き不足、権限）は元を残し、その退避元だけを保留にする。`archive.failed` は最初の 1 回と 24 巡ごと。同じ原因のカードは 1 件。
- 元の除去: ホストのパスの退避元は archiver が消さない。`remove_original: true` の退避元だけ、照合済みの manifest を `${ax_state_dir}/archive/verified/<名>/<日付>.json` に書く。AX のボリュームの `archive/` は、印 `reap-enabled` がある間だけ、除去の直前に大きさ・mtime・inode を再確認して archiver が移す。
- 印 `reap-enabled` は b1 では作らない。有効化は b17（FACTS.md の F-20 の「有効化」）。
- `subpath` の実機判定は未実行。判定のロジックは `core/archiver/subpath_probe.py`。

## 起動と設定

- 環境変数と既定値は各 `app.py` の冒頭に書いた。値は compose の `environment` が渡す。実在のホスト名やアドレスは、コードに埋め込まない。
- 起動: `host/ax-up.sh timeline memory-connector archiver`（B0 の最小版）。`archiver` は鍵 `archive-ssh-key` と `archive-known-hosts` が要る（B2、go の後）。
- 他の束のボリュームを退避の対象に加える方法と、`/src/<名>` の bind の作り方は `archive-volumes.md`。

## 試験

- `tests/b1/`。偽の部品は `tests/fakes/b1/`（偽の退避先、偽の MCP、事象の作り方）。ネットワークとサービスは使わない。
- 偽の退避先は本物の `rsync` をローカルで動かし、`rrsync` の書き込み先の制限とシェルの拒否を模擬する。
- HTTP の試験（`*_http.py`）は starlette と httpx がある環境で動く。ない環境では、その 3 ファイルだけを飛ばす。

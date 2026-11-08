# オブザーバー

`python -m core.observer`。`/healthz`（8080）だけを開く。`ax-core` の網だけ。ports は持たない。

## 周期

`observe_s` ごとに `Observer.run_once()` が次を行う。1 つの失敗で他を止めない。

1. `/state/observer.beat` に時刻を書く（ホスト見張りが読む）。
2. 切り替えの記録の取り込み（[cutover.md](cutover.md)）。
3. ホスト見張りのログ `/state/hostwatch.log` の写し。`host.sudo_unavailable` のカードと `deploy.rolled_back` の ToDo もここで作る。
4. 時系列記録の `health` 投影と、`error.classified`、`manager.cycle`、`manager.decision`、`inspection.phase` の新しい事象を取り込む。
5. 検査を全部回す。警報は (check, target) ごとに `alert_dedup_min` に 1 回だけ出す。

## 検査と宛先

| check | 条件 | target | 宛先 |
|---|---|---|---|
| `liveness` | beat が `stale_beats` × `heartbeat_s` 秒ない。時系列記録が落ちているときは `/healthz` を直接見る | 部品名 | プラグインは管理役。中核はホスト見張り（要求 `restart`）と管理役 |
| `watch` | 見張り用ノードの警報 | サブループ | 管理役。**差し込み口 `watch_signal` が無い間は未実装**（OPEN.md） |
| `expired_pending` | `expired_pending` が `expired_grace_min` を超えた | 命令の ID | 出した者。管理役が出した物は予備役 |
| `lag` | `health.beat` の `lag_s` が `lag_alert_s` を超えた | 部品名 | 管理役 |
| `starve` | 役割に `queued` の job があり、その役割のどの job も `starve_min` 分更新されない | 役割 | 管理役 |
| `repeat` | 同じ部品・同じ `code` の `error.classified` が 1 時間に `repeat_threshold` 回を超えた | `部品:code` | 管理役 |
| `manager_trouble` | `silent`（`manager.cycle` が `manager_silent_min` 分ない）、`bad_streak`（`bad_output` が `manager_bad_streak` 回続いた）、`unknown_unhandled` | 左の3つ | 予備役 |
| `drain_silent` | 点検の減らし込みの間、管理役が `drain_silent_min` 分動かない | `inspection` | 点検係（「再開」） |

警報は `health.alert`（`check`、`target`、`severity`、`destination`、`detail`）として、宛先ごとに 1 件書く。

## 差し込み口（他の束の拡張）

`core/observer/ext/<束の ID>.py` の `register(app)` から `app.state.observer` を使う。

- `add_check(name, fn)`: 検査を足す。`fn(ctx) -> list[Alert]`。
- `set_hook("backup", fn)`: 予備役の呼び出し（S-11.3）。オブザーバーが先に `backup.activated` を書き、`fn(alert)` を呼ぶ。
- `set_hook("inspection", fn)`: 点検係への「再開」の指示。
- `watch_signal = fn`: 見張り用ノードの警報を返す。

`ext/b10a.py`（予備役）は b10a が置く。この束は置かない。

## 環境変数

`core/observer/settings.py` の先頭に一覧がある。値は `compose.d/b8.yaml` が渡す。

## ホストの bind と再起動

`/state` 全体は読み取り専用とする。`host-requests/` と `observer.beat` だけ書き込みを許す。`cutover/` は別の読み取り専用 bind とする。
ホスト側の起動手順は、observer を起動する前に `${ax_state_dir}/observer.beat` を operator が書ける通常ファイルとして用意する。存在しないパスを Docker にディレクトリとして作らせないため、`create_host_path: false` を指定する。
beat は同じ inode に書き、ホストの読み手と共有する。重複除去の表は `/data/dedup.json` に残る。検査用の揮発状態は、再起動時に直近 `feed_backfill` 件の事象を読み直して作る。

## ホストログと台帳

`hostwatch.log` の sudo 不調・復旧と配備を前のタグへ戻した事象を取り込む。カード・ToDo は元の事象の ID を `id` と `source.event_id` に使う。送信後に応答を失っても、同じ ID で再送する。台帳の `409 already_exists` は作成済みとして扱う。
同じ sudo 不調の間はカードを1件にまとめる。時系列記録が停止していても、ホストログからカードを作る。ログの再取り込みで副作用を重ねないよう、処理した事象の ID を `/data/host_effects.json` に残す。
台帳への書き込みには compose の secret `core-token-observer` を使う。値はログに出さない。

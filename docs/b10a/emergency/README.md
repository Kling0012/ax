# 緊急ボタン

緊急ボタンは状態を退避し、自律側の命令を解除して再開する。仕様は S-12.3、S-22.8、D-11.3、試験は T-15 に対応する。

## 操作経路

HQ の操作は `IntakeClient.submit` から入口の行き先 `emergency` に渡す。入口で確かめた資格が `from=hq`、`via=hq` の場合だけ扱う。登録簿の段は `irreversible`、応答は `async` とする。

HQ の確認を経た指示には `confirm_irreversible: true` と `idempotency_key` を付ける。承認の保存と照合は入口の承認機能が担う。確認のない指示は承認待ちとなり、緊急処理を始めない。緊急サービスを直接呼んでも、HQ の本人確認や承認の代わりにはならない。

入口のハンドラは `payload.scope` を `POST /v1/emergency` の `scope` に渡す。`idempotency_key` は同じ値の `request_id` とする。資格は `core-token-intake` から読む。サービス側は core-tokens の表で呼び出し元が `intake` と決まる場合だけ通す。表が使えなければ 503、それ以外の呼び出し元は 401 とする。

## 範囲

| scope | 命令の解除 | 再起動の要求 |
|---|---|---|
| `all` | `manager`、`backup`、`inspection` の有効な命令と `expired_pending` の命令 | ホスト見張りに登録されたサービス |
| プラグイン ID | 上と同じ発行者で、その ID を対象とする命令 | 登録済みの `sl-<ID>` または `<ID>` |
| `commanders` | `commander` の命令。入口の lockdown も呼ぶ | 行わない |

`issued_by=user` の命令は解除しない。プラグインを指定した操作では、ほかのプラグインや `target=all` の命令も解除しない。プラグインは登録簿と照合する。制御 API が取得不能の場合は照合できない状態で処理を続け、その失敗を結果に残す。

`commanders` の lockdown は、入口の `app.state.intake.lockdown(enabled, ident)` で接続する。この口が未実装なら `lockdown.done: false` と `lockdown_not_implemented` を返す。命令解除の結果だけで、席・保持・承認待ちの停止が済んだとは扱わない。司令塔や Paseo のプロセスと設定は変更しない。

## 記録から再開まで

1. timeline の `POST /v1/export`、制御 API の命令一覧、台帳の ToDo、worker-pool のジョブ一覧を取得する。`/data/archive/emergency-<時刻>-<ULID>/` に保存し、`emergency.snapshot` を書く。
2. 範囲内で解除できる命令だけを解除する。再起動はホスト要求ファイルを通して依頼し、`emergency.reset` を書く。
3. 各プラグインの effective と manifest の既定の mode を照合し、`emergency.resumed` を書く。利用者命令などで mode が残る場合は `held_by` に示す。

スナップショットには `timeline-export.json`、`commands.json`、`todos.json`、`jobs.json`、`manifest.json` を置く。timeline の export API が返す `last_seq` を記録の上限にする。`GET /v1/events` をページごとに読み、その上限までの事象を `timeline-export.json` の `events` にまとめる。API が返した書き出しファイルの参照情報も残す。途中の取得が失敗した場合は完全な記録として扱わない。

各記録は秘密検査を通す。検査できない記録や取得できない部品は、その節の失敗として残す。命令の解除とほかの節の記録は続ける。退避先が書けなければ `snapshot.complete: false` とする。完了した外部操作の push やコメントを取り消さず、実行中の worker-pool のジョブも取り消さない。

同じ `request_id` の再送は保存済みの結果を返す。範囲が異なる再送は 409 で拒否する。処理が同時に来た場合も 409 とする。完了結果を保存する前のプロセス停止では再送による再処理がありうる。命令の解除と既存のホスト要求の照合は繰り返しを考慮するが、再起動そのものの完了確認はこの応答には含めない。

## ホスト要求と sudo

ホスト要求は共通の `core.observer.hostreq.write_request` で書く。`/state/host-requests/<ULID>.req` の中身は、操作 `restart` とサービス名 `name` だけとする。イメージ、タグ、任意のパスは指定しない。ホスト見張りが登録済みサービスと照らし、特権操作を実行する。

`/state/hostwatch.log` の最後の sudo 状態が `host.sudo_unavailable` なら、再起動の要求だけを省く。命令の解除は続ける。状態が不明な場合は要求を書き、実行できるかはホスト見張りが判断する。`host.sudo_restored` を読んだあとの操作では再び要求を書く。

`emergency.resumed` は命令の解除後の mode と要求の発行結果を示す。再起動を実行した証明ではない。実行結果はホスト見張りの記録で確認し、部品の起動後の照合は S-3.4 に従う。

## 配備の前提

`compose.d/b10a.yaml` は B0 の共通イメージと網、B1 の退避機能、B5 の入口、B8 の observer、B10b の HQ の基本定義に重ねる。`emergency` は ax-core の内部の 8080 を使い、ホストへポートを公開しない。Docker のソケットと sudo は渡さない。

| 設定・マウント | 用途 |
|---|---|
| `emergency-data:/data` | スナップショット、完了結果、outbox |
| `${AX_STATE_DIR}:/state` の読み取り専用 bind | ホストの登録一覧、sudo 状態、資格のハッシュ表、秘密検査の索引 |
| `${AX_STATE_DIR}/host-requests` の書き込み bind | ホストへの再起動要求 |
| `control-token-emergency` | 命令解除の資格 |
| `core-token-intake` | 入口から緊急サービスを呼ぶ資格 |
| `core-token-observer` と `control-token-backup` | 予備役の台帳・制御 API の資格 |
| `AX_REDACT_INDEX` と `pii-list` | 退避する記録の秘密と個人情報の検査 |
| `AX_NTFY_ENDPOINT` と `AX_NTFY_TOPIC` | HQ の通知先。profile から渡す |
| `AX_HQ_DATA` と `AX_HQ_NOTIFY_RUN` | HQ の通知状態の保存先と配送処理 |

ホスト見張りへの登録、資格の発行、HQ の基本定義、archive の初期化と運搬経路を配備時にそろえる。秘密の値は設定や手順書に書かない。

## 検証

ネットワークとサービスを使わない試験は次のコマンドで実行する。`tmp_path` は `$TMPDIR` の下を使う。共通 fixture はソケットの接続を拒否する。

```sh
python3 -m pytest -p no:cacheprovider tests/b10a/emergency
```

試験では記録の順序、全範囲とプラグイン範囲、利用者命令の保持、外部操作と実行中ジョブの保持、部分障害、sudo 不可、秘密検査、再送、資格、HQ の入口を確かめる。ホスト要求は一時ディレクトリだけに書く。

Compose は `tools/ci-fixtures/ci.env` のダミー値を使い、リポジトリの根で検査する。デーモンを必要としない。

```sh
docker compose -p ax-ci --env-file tools/ci-fixtures/ci.env -f compose.yaml -f compose.d/b0.yaml -f compose.d/b1.yaml -f compose.d/b5.yaml -f compose.d/b8.yaml -f compose.d/b10a.yaml config --quiet
```

この組合せでは HQ の基本定義も必要になる。B10b が完成したらその compose 定義を加える。基本定義がない間は `hq` の image/build 不足で失敗する。担当の compose 試験は、一時ディレクトリに HQ のダミー基本定義を置いて構成を解決する。実際の HQ の配備を検証した結果には含めない。

| 実環境で確認する項目 | 進める段 | この成果物づくりでの扱い |
|---|---|---|
| HQ の本人確認・承認から実際の入口まで | B10b・B12b の接続後 | 未実行。担当範囲では ASGI と偽の転送で確認する |
| lockdown による席・保持・承認待ちの停止 | B12b の接続後 | 未実行。入口の実装を必要とする |
| ホスト見張りの要求受理と登録済みコンテナの再起動 | B17 | 未実行。実機とサービスを操作しない |
| sudo 不可と復旧の実際の挙動 | B17 | 未実行。一時ログで判定だけを確認する |
| 再起動後の照合と二重実行の防止 | B17 | 未実行。実行系の起動を必要とする |
| emergency と timeline の退避、取り出し、権限 | B17 | 未実行。退避先への転送を行わない |
| 実際の ntfy のテスト通知 T-55 | B17 | 未実行。通知配送担当の手順に従う |

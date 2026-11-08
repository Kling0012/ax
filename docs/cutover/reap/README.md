# reap の有効化と停止

切り替えの実施者がホストで使う道具である。F-15 の段6と、S-14.3・D-22.2 の停止・再開を受け持つ。管理役・予備役・点検役からは呼ばない。

## 共通実行器との接続

`tools.cutover.reap.common_adapter.from_context` に b17-1 の `Context` を渡す。承認と保持は `Context.guard()`、コマンドは `Context.run()`、記録は `Context.note()` に集約する。dry-run の Context はファイルを変更しない。サービスを直接呼ぶ既定値はない。

```python
from tools.cutover.reap.common_adapter import ReapBackend, from_context
from tools.cutover.stages import Operator

# ctx は b17-1 の実施者 Context。
# sources_file、changelog は配備先の絶対パス。
# restart_argv は既存 profile と compose の指定を含む archiver 再起動の argv。
reap = from_context(ctx, sources_file=sources_file, changelog=changelog,
                    restart_argv=restart_argv)
backend = ReapBackend(existing_backend, reap, t2_check=t2_check)
result = Operator(ctx, backend).advance(6)
```

`existing_backend` は他の段と `copy_cutover_log` を実装する b17 の既存アダプタである。`t2_check` は B2 の T-2 の合格結果を確認して真偽値を返す。実施していない T-2 を true にしない。段6は `enable_reap` が条件未達なら例外で保留し、成功の記録を付けない。

配置先の値は profile と実施者の Context から受け取る。ホスト名、アドレス、鍵の値をコードへ埋め込まない。退避用の鍵を作る処理、旧 unit を止める処理、AX の制御 API、timer の action はこの道具に持たせない。

## 有効化

1. F-15 の段4で、旧 `archive-to-hdd.service` と `.timer` の停止・無効化を済ませる。この道具は両方について `is-active` が inactive、`is-enabled` が disabled または masked であることを確かめる。実行中や有効のままなら、設定も印も変更しない。
2. T-2 の合格、同じホストの切り替え記録にある最新の T-78 の合格、登録した各退避元の `verified/` に照合済み manifest があることを確かめる。manifest は既存スキーマで検査する。処理済みの `.reap.json` は合格の根拠にしない。
3. 元の `archive-sources.yaml` を撤去の archive の `reap-enable-<UTC日時>/` に複製する。`mp-runs-archive`、`ci-public-logs`、`issue-automation-wt`、`codex-runs` の登録分だけを true にする。`claude-projects` は false にする。
4. 共通実行器で archiver を再起動する。再起動に失敗したら印を作らず、コピーだけを続ける。保持と旧 unit の状態をもう一度確かめる。
5. `archive/reap-activation.json` に実施の記録を残し、空の `archive/reap-enabled` を作る。続いて指定された `docs/CHANGELOG.md` に日時と対象を追記する。実施の記録は二重の有効化を防ぐ。停止後に段6を再実行しても印は作り直さない。

条件が不足している間は `waiting`、実施中の失敗は `failed` を返す。他の機能やコピーを止めない。設定の複製と次の設定は archive に残る。

印の作成後に CHANGELOG の追記が失敗した場合は `enable_incomplete_marker_present` を返す。この場合は印がある。成功扱いせず、実施者が記録の追記を完了する。印の作成前に中断し、実施の記録だけが残った場合も自動で作り直さない。実施者が現物と archive を確認する。

## 停止

```python
stopped = reap.stop(reason="emergency")
# 退避の途中で不整合を見つけた場合は reason="inconsistency"。
```

停止理由は上の2つだけ受け付ける。通常の巡回を理由に停止しない。

1. 保持を確認する。
2. `archive/verified/` を撤去の archive の `reap-stop-<UTC日時>/verified/` に複製する。元の manifest は残す。リンクと通常ファイル・ディレクトリ以外の物は受け付けない。
3. 複製が終わった後で `reap-enabled` を同じ archive へ移す。削除しない。
4. 共通記録口に `cutover.state`、subject `reap-stop`、target `reap/<ホスト>` を書く。

複製に失敗したら印を移さない。記録に失敗した場合は `stopped_note_failed` を返す。この場合も印は archive にあり、reap は停止している。実施者が保持と停止状態を確認し、共通の `Context.note()` で不足した記録を追記する。

## 再開

```python
resumed = reap.resume(stopped.archive)
```

この切り替えの archive 内にある停止の写しだけを受け付ける。保持を確認し、空の印を元へ戻して `reap-resume` を記録する。再開を承認する別の問いは追加しない。

この道具は元のファイルを除去しない。次のホスト見張りの周期で、既存の `host/ax-archive-reap.sh` が現在の manifest と大きさ・mtime_ns・inode を再確認する。停止中に変わったファイルは残す。記録だけに失敗した場合は `resumed_note_failed` を返す。印は元の場所にあるため、共通記録口で不足した記録を追記する。

## T-78 の実機手順

1. B0〜B16 と、B2 の T-2 を完了する。専用鍵、known_hosts、退避元の除外、profile の `archive_dest`、F-18.1 の通信制限を確認する。鍵の値を出力しない。
2. 既存 archiver の `Transport` を、既存 profile と専用鍵から作る。汎用の root 鍵を渡さない。
3. `claude-projects` の7日より古い1ファイルを選ぶ。`remove_original` は false、reap の印は無しのままとする。
4. `reap.verify_t78(transport, relative_path=relative_path)` を実施する。既存 Transport の `rsync -a --checksum` と再度の `--dry-run --itemize-changes` で内容を照合する。照合後に元が変わっていないことも確かめる。
5. 同じ大きさでも内容が違う、照合不能、転送失敗なら failed を記録し、元を残す。成功時もこの試験では元を残す。切り替えの記録が失敗した場合は合格として次へ進まない。
6. 実機では、同じ制限鍵が退避先の外とシェルを拒否することも確認する。結果を B17 の実機結果へ渡す。

この成果物の作成中は archive-host への転送、鍵の実動作、実機の reap を実施していない。

## 検証の範囲

`tests/b17/reap/` はソケットとサービスを使わない。T-78 の模擬退避先はコピー先の内容を SHA-256 で照合する。同サイズの内容不一致も試す。既存 archiver の失敗時の保持も確認する。

T-125 と T-154 は一時ディレクトリのホストと、既存 `host/ax-archive-reap.sh` の変更していない Python 本体で確認する。shell の入口と flock はこの試験では実行しない。段6と停止・再開は共通 Context の偽実行器でも通す。

実機で残る確認は [realenv.json](realenv.json) にある。b17-1 の status 集約へ渡す。

## 未解決の依存

F-20 は、有効化の前に各退避元の `verified/` に manifest があることを求める。一方、現在の既存 archiver は host の退避元で `remove_original` が true の場合だけ `verified/` に書き、false の場合は `archive/manifests/` に書く。初期設定の false のままでは前提を満たせず、`claude-projects` も false のまま維持する。

この道具は別の置き場の manifest を照合済みと見なさず、有効化を保留する。b1 の担当側で manifest の生成経路を解決し、F-20 の条件を満たす必要がある。担当外の archiver は変更していない。

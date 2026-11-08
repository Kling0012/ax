# 旧部品の撤去と停止集合

段4の実施者が使う道具である。AX の常駐部品には組み込まない。import だけではコマンドを実行しない。
順序は SPEC S-24、停止集合は S-24.2、実パスと移動先は FACTS F-4.2〜F-4.5、F-5.1 に従う。

## 呼び出し口

`tools.cutover.retirement.run_retirement(context, profile)` は、旧 issue-automation の G1〜G11、残りの停止集合、条件付きの旧退避 timer を処理する。
実行器・記録口・カード作成・go と保持の確認をすべて引数で受ける。既定の実行器はない。

| 引数 | 内容 |
|---|---|
| `Context.execute(host, argv, stdin)` | ホスト別の実行器。`CommandResult(returncode, stdout, stderr)` を返す。到達不能は例外か終了コード255で示す |
| `Context.record(kind, subject, data)` | 保持を確認した追記専用の記録口。`cutover.state`、`removal.state`、`removal.check` を受ける |
| `Context.latest(subject)` | 信頼できる記録から最新の事象を返す。記録なしは `None` |
| `Context.card(key, title, body)` | 同じ key のカードを重複して作らない口 |
| `Context.authorize()` | 実際の承認と保持を確認する。拒否時は例外で止める |
| `Context.verify_compute-host_replacement(host)` | B15/B17 で T-142 の実行物を確かめた結果を返す。true のときだけ G9 を許す |
| `Profile.local / legacy / compute-host` | 3ホストの `Host`。接続名、ホーム、unit の場所、実行物の場所、runtime の場所、uid をプロファイルから渡す |
| `Profile.day` | 検証済みの撤去 `GO.md` があるディレクトリの日付。`YYYYMMDD` |
| `Profile.preconditions` | `PRECONDITIONS` の全項目を実際に確かめた結果。1つでも欠ければ停止前に拒否する |

compute-host の `Host.disk_root` には別ディスクの根を渡す。各ホストの値を操作元の HOME から補わない。
`Profile.optional_unmount` は既定で false。true のときだけ G10 の fstab と umount を扱う。umount の失敗は記録に残して続ける。
待機は既定で最大1800秒、60秒間隔である。試験では注入した時計で短縮する。

## 共通実行器との接続

`common_adapter.from_common()` は b17-1 の `common.Context.guard()` と `note()` を使う。
席の ID、接続の ID、保持の事象 ID は共通記録口に任せる。独自に生成して書かない。
ホストの変更権限は local が F-15.2 の7番、legacy と compute-host が12番で確認する。
共通 Context が dry-run のときは実行を拒否する。

`common_adapter.common_executor(ctx, profile, routes)` は共通 runner への変換を担う。
`routes` はホスト名ごとの明示的な引数列である。その場の実行は空の列、遠隔の実行はプロファイルから得た SSH の引数列を渡す。
遠隔コマンドは `shlex.join()` で1つの文字列にする。ファイル操作の JSON 入力は Python の文字列リテラルとして渡す。
この変換器も呼び出されるまで接続しない。

段4の backend では同じ `RetirementSession` を次の順で使う。

```python
from tools.cutover.retirement.common_adapter import (
    RetirementSession, common_executor, from_common,
)

execute = common_executor(common_context, retirement_profile, routes)
ports = from_common(
    common_context, retirement_profile, execute=execute, card=create_card_once,
    verify_compute-host_replacement=check_actual_t142,
)
session = RetirementSession.create(ports, retirement_profile)
others = session.retire_others()                 # retire_others
result = session.retire_archive_to_hdd()         # retire_archive_to_hdd
```

`retire_archive_to_hdd()` は `retire_others()` の前には呼べない。
T-142 の証拠のファイル名や事象の形は仮定しない。`check_actual_t142` は、B15/B17 の実機確認に接続する。接続が未実装なら `NotImplementedError` とし、G9 だけを飛ばす。
T-78 が失敗した場合、段4の実施者は後者を呼ばず、既存の段4の分岐でカードを作る。一括の `run_retirement()` はこのカードも作る。
結果の `condition1` は停止集合の確認結果である。true でも巡回は再開しない。
b17-1 が条件2〜5、既存 PR・Issue の一覧、一覧の副作用0件を確かめて再開する。
元の除去の有効化は b17-3 の段6に任せる。T-78 と旧退避の停止だけを根拠に有効化しない。

## 順序と失敗

G1 は compute-host の掃除 timer、local の旧 issue-automation timer、legacy の状態確認の順である。
停止前に既存の公開・poll ロックを検査する。保持中なら、どのホストも停止・移動しない。
その後は G2、G3、G4・G5、G6、G7・G8・G9、G10、G11 の順に進む。
各 G の対象ホストごとに1件ずつ結果を書く。対象は合計20件である。任意の G10 を選ばなければ `skipped / optional` と記録する。

| 状況 | 動作 |
|---|---|
| G2 の待機が上限を超えた | `waiting` とカードを残す。そのホストの後続だけを止め、処理を強制停止しない |
| compute-host が到達不能 | compute-host の G3 は `failed`、G6 と G9 は `skipped`。他の2ホストを続ける |
| compute-host の G3 が失敗 | compute-host の unit と置き場を移さない。他の2ホストを続ける |
| T-142 の確認が未合格・未接続 | G9 を `skipped / t142` にし、カードを作る。他の撤去を続ける |
| 別のファイルシステム・移動先の衝突 | そのホストの移動を止める。複製して消す処理に切り替えない |
| T-78 の最新記録が `ok` | 実行中とロックの解放を待ち、停止集合の最後に旧 `archive-to-hdd` を止める |
| T-78 が失敗・記録なし | 旧退避の unit を残す。他は続ける。`condition1=false`、`next_step=collect_only` を返す |

legacy の同名の `archive-to-hdd`、保持対象のサービス、nft の表、Docker イメージ、ユーザーには触れない。
旧 X timer 3組は段4の前提がそろってから止める。旧キュー・送信記録・カーソルを読まず、送信もしない。
候補 ID の分離は B16 の担当であり、この道具は ID を生成しない。

## 移動と記録

移動には同じファイルシステム内の `os.rename()` だけを使う。compute-host の別ディスクの物は同じディスク内の行き先へ移す。
ゲームデータと NuGet のキャッシュだけを AX の置き場へ引き継ぐ。空になった旧ディレクトリも削除しない。
system の unit、sbin、fstab の操作と退避物の検査には、注入した実行器経由で `sudo -n` を使う。
退避先に新しく作る記録とディレクトリは、親の所有者を保つ。

各ホストの `<A>/issue-automation-old/manifest.tsv` にホスト、元のパス、移動先、通常ファイル数、見かけのバイト数を残す。
バイト数にはディレクトリとリンク自身を含め、ハードリンクの内容は1回だけ数える。
移動前後の値を照合し、同じ行を重複して書かない。移動前に `move-intents.jsonl` へ書き、移動直後の切断でも次回に manifest を補える。
`<A>/README.md` には元の場所と戻し方を残す。実行物の git commit は移した追跡パスだけに限定し、push はしない。

旧部品を自動では戻さない。利用者の個別指示がある場合だけ、README と manifest を参照する。
戻す前に AX の該当する外部処理を `collect_only` にする。compute-host の置き場を戻す前に掃除 timer を有効にしない。
旧 issue-automation は `enabled=false` と dry-run の確認を経てから timer を有効にする。戻し試験は要求しない。

## 検証

担当試験は次の範囲に限定する。

```sh
python3 -m pytest -p no:cacheprovider tests/b17/retirement
```

| 試験 | 模擬環境で確かめる範囲 | 実機で残る確認 |
|---|---|---|
| T-141 | G1〜G11 の上下、20件の記録、実ロック、移動件数、manifest、削除の呼び出し0件 | 各ホストの残存プロセス、unit、nft、常駐コンテナ、移動後の内容 |
| T-143 | 到達不能・待機超過・G3失敗・別ディスク・T-142未合格・umount失敗の局所化 | SSH、sudo、実マウントの権限と挙動 |
| T-144 | 構築前の呼び出し拒否、段4での3組の停止、旧送信記録を読まないこと | B16 の候補 ID と、構築前後の実 unit の状態 |
| T-149 | T-78 の最新記録による分岐、最後の停止、ロック待機、再実行、共通段4の2つの呼び出し | 実 T-78、条件3を経た巡回再開、b17-3 の元の除去の1回の有効化 |

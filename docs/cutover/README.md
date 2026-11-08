# AX 切り替え手順

実施者が F-15 の段を一つずつ進める。入口は `host/ax-cutover.py`、共通部は `tools/cutover/common.py` にある。サービスとして起動しない。引数を省いたときも `--dry-run` を付けたときも、計画だけを返す。

```sh
python3 host/ax-cutover.py --profile <profile.yaml> --archive <撤去のarchiveの絶対パス> --stage 1
```

計画の `result: planned` は試験や作業の合格を表さない。実施者は、この成果物を作る間は `--apply` を使わない。実機での構築、起動、停止、移動、資格の変更は行っていない。

## 実施前の確認

B0〜B16 の構築と担当試験が済んでいることを確かめる。F-16 の現物確認と S-16.6 の登録を済ませる。ホスト名、接続先、パスは配置先のプロファイルから読む。本書の例に本物の値を埋めて配布しない。

GO.md を実施者が作ったり直したりしない。F-15.1 に従い、対話セッションが受けた構築の go を追認し、利用者が HQ で撤去を承認する。AX_SESSION は手順を通して同じ値にする。

```sh
ax go ratify <構築のGO.md>
ax approval wait <追認の承認ID>
ax go removal --scope both --changes <了承した番号> --ratify <追認の承認ID>
ax approval wait <撤去の承認ID>
ax approval export-go <撤去の承認ID> --out <撤去のarchive>
ax approval verify <撤去の承認ID> --go-file <撤去のGO.md> --build-go <構築のGO.md>
ax claim acquire cutover
```

`go ratify` と `go removal` が返す終了コード4は承認待ちを表す。利用者が HQ で全文を読み、第二要素を入力して承認するまで次へ進まない。構築の go も承認経路で取っており、構築の GO.md が無い場合だけ `--ratify` と `--build-go` を省く。構築だけの go では撤去を始めない。GO.md の存在だけでも始めない。

撤去の archive は F-5.1 の `~/.local/share/ax/archive/ax-cutover-<UTC日付>/` にする。旧 `mp-runs/archive` の中に置かない。構築の GO.md と撤去の GO.md は別の原本として保管する。同じ名前を上書きしない。

## 実行器の接続

既定の実行器は実機処理を拒否する。`--apply` では、承認と保持を確認してから、実施者が確認したローカルの `module:factory` を `--adapter` で読み込む。接続先を推測する処理はない。副パッケージの完成後に接続し、接続した構成で模擬試験を通す。詳細は [共通部の接続](interface.md) を参照する。

```sh
python3 host/ax-cutover.py --profile <profile.yaml> --archive <撤去のarchive> --stage <1〜6> --apply --go-file <撤去のGO.md> --approval-id <撤去の承認ID> --build-go <構築のGO.md> --adapter <確認済みモジュール:factory>
```

一度の呼び出しで一段だけ進める。出力は `ok`、`waiting`、`planned` のいずれかである。前提や接続が不足すると `blocked` と終了コード3を返す。`waiting` は終了コード4になる。秘密を含み得る例外やコマンド出力は表示しない。

## 段1 原本の複製

settings.json、CLAUDE.md、RTK.md、全プロジェクトの MEMORY.md、.claude.json、rules/、hub/ROLE.md を編集前に複製する。F-5.1 の分け方で保存し、読めることを確かめる。同名の控えに異なる内容があれば上書きせず止める。完了後に `orig-copy` を追記する。

## 段2 新しい入口

監視を最初に起動し、中核、新入口の順で進める。手引きから生成した CLAUDE.md を置き、<EMAIL>` が1行あり、RTK.md 自体が変わっていないことを確かめる。MCP の magpie 登録は残す。F-5.4 の差し替え先は ax コマンドであり、MCP 設定の編集は不要である。

AX の監視が S-24 の4条件を満たし、commander のソケットから ax status が HTTP 200 を返すことを確かめる。監視を起動し直したら、起動後の2周回を数え直す。B17 の実機試験を行い、T-78 以外に未実行や失敗があれば、この段で止める。T-78 は結果を追記し、失敗でも他の撤去を進める。

T-58 が通ったら、8907 の転送先だけを新しい HQ のソケットへ向ける。向け先と tailnet 直接入口の HTTP 200 を確認し、`entry-placed` を追記する。旧 HQ はまだ停止しない。公開入口の利用者による確認は段5に分ける。

段2で失敗したら旧入口を外さず、旧部品も止めない。原因を直して段2をやり直す。

## 段3 旧入口の除去

新入口と監視、ax status、HQ の向け先を再確認してから pending-guard.py の hook を外す。dev-flow-hook.py、RTK の hook、MCP の magpie 登録は残す。旧 rules/roles、models-routing.md、hub/ROLE.md を削除せず archive へ移す。storage.md の退避先の1行を F-5.3 の文面に直す。他の行は変えない。

MEMORY.md は b17-3 の memory_split.py に渡す。仕分けは S-24.3 に従う。迷う項目は残し、失敗したファイルは書き換えず他のファイルを続ける。旧ワークフロー規則を新しい判断根拠として取り込まない。

## 段4 旧部品の停止と巡回

停止前に、実行中が0、初日の登録と接続、AX の正常な監視、新入口、HQ の向け替えを確かめる。「旧部品がすべて停止済み」は停止前の条件にしない。

b17-2 の撤去器で S-24.2 の集合を扱う。G1〜G11 の順序は S-24 手順4aに従う。compute-host の掃除 timer を最初に止める。各ホストの失敗はその手順と後続だけに留める。Paseo、Cloudflare、認証、agent-memory、hub-ntfy、OS の保持対象に停止を広げない。

T-78 の最新の結果が ok なら、他の timer の後に archive-to-hdd を止める。停止前に service の inactive と非待機の flock を確認する。実行中なら最大30分待ち、超えたらカードにする。T-78 が failed または記録なしなら archive-to-hdd に触れず、他の撤去を進める。巡回と reap は有効にしない。

旧台帳の書き込み側が停止してから、todo.sqlite と存在する -wal、-shm を複製し、SQLite で読めることを確かめる。新台帳には「旧 ToDo の扱いを決める」を一定の重複防止キーで1件だけ作る。旧 ToDo と旧処理状態は取り込まない。

条件1、2、4、5を満たしたら、一覧の作成直前に `cutover-time` を追記する。開いている Issue・PR の状態、ラベル、最後のコメント時刻を collect_only で集め、時系列記録へ残す。作成中の merge、コメント、ラベル変更、作業起動は0件にする。一覧と cutover-time が時系列記録で読め、5条件がすべてそろってから通常巡回へ移る。X・Discord・scheduled-publisher は対象を指定した開始指示まで collect_only のままにする。旧キューや旧送信記録を読まず、古い未投稿候補を送らない。

## 段5 公開入口の確認

利用者が公開入口を1回開く。失敗時は hq-access-email のカードで直す。この結果は旧 hq.service の停止条件にしない。直るまでは tailnet 直接入口とホストの ax を使う。

## 段6 reap の有効化

T-78 が ok で、旧 archive-to-hdd が停止・無効になっていることを再確認する。b17-4 の reap 手順で対象4つだけの remove_original と reap-enabled を有効にする。claude-projects はコピーだけを保つ。一度有効にした後は enable を繰り返さず、停止と再開の専用手順を使う。

最後に cutover-state.jsonl を撤去の archive の cutover/ に複製する。元の記録を消さない。

## 失敗後の扱い

旧入口を外した後に不調が出ても、旧部品を自動で再起動しない。AX 側を直す。ホストの ax、ホスト見張り、compose logs を窓口にする。archive から戻すのは利用者が個別に指示した場合だけである。戻した物と時刻を archive の README.md に残す。T-72 と T-75 は廃止されており、状態遷移、依存確認、復元確認を追加しない。

保持を失ったら新しい操作と記録を始めない。次の実施者は lease が切れた後に保持を取り、追記記録と現物を確認して一段ずつ続ける。記録だけで現物の正常を推測しない。

# Windows ツールの呼び出し口

S-25.9 の Windows Computer Use を扱う。ツールは環境台帳にもモデル台帳にも登録しない。`env.run`、`requires`、能力、ワーカーの候補から選ぶ経路は持たない。

初期の候補は `remote-device-a` と `remote-device-b` である。現物は未確認であり、既定の実行器は接続も操作もしない。未実装の間はその呼び出しに `unavailable` と理由 `not_implemented` を返す。

## 設定

設定は `config/windows-tools.yaml` から読む。`load_tools()` は `AX_CONFIG_DIR` の下を読む。検証器には既存の `AX_SCHEMAS_DIR` を使う。設定に接続先の値、資格、能力、モデルの選択条件は入れない。

| 項目 | 内容 |
|---|---|
| `id` | 明示して呼ぶツール ID |
| `kind` | `windows-computer-use` |
| `location` | プロファイル内の変数名 |
| `max_concurrent` | 常に 1 |
| `owner_use` | 普段使いの機かを示す登録情報 |
| `data_classes` | ツールが許す区分。初期値は `public` のみ |

接続先は呼び出し口に渡す `profile` から解決する。変数名は `windows_remote-device-a_target` と `windows_remote-device-b_target` である。値はプロファイルだけに置く。変数が無ければ `profile_missing` を返す。ツール ID、参照する変数名、解決した接続先に重複があれば登録を拒否する。

S-25.9 と D-23 の `config/tools.yaml` に差し込む形式は未定義である。`register_tools_config()` は `NotImplementedError` を出す。設定を別名で複製したり、環境台帳に入れたりしない。

## b18b-4 からの呼び出し

`core.resources.winwork.WindowsTools` を資源サービスのツール用 API から呼ぶ。環境 API の経路へ登録しない。HTTP のパス、認証、応答の対応づけはこの副パッケージでは定義しない。

組み立てには `tools`、`profile`、`state_dir`、`events` を渡す。`executor` を省略すると `UnconfiguredExecutor` を使う。`events` には標準の `EventSink` を渡す。`state_dir` は resources の永続ボリューム内の専用ディレクトリとし、すべてのインスタンスで共有する。

`call(request)` の入力はこの副パッケージ内の形である。HTTP API の仕様ではない。

```json
{
  "tool_id": "remote-device-b",
  "target": {"kind": "app", "value": "Notepad"},
  "instruction": "明示したアプリで依頼された作業を行う",
  "sensitivity": "public",
  "user_requested": true
}
```

`target.kind` は `app` または `url` とする。`url` は絶対 HTTP(S) URL を指定する。空の ID、空白だけの行き先、未登録の ID、行き先を欠いた依頼は拒否する。`user_requested` が真でも ID と行き先は省けない。依頼に接続先やモデルの情報を足して判定を上書きすることもできない。

返り値は `tool_id`、`status`、`reason` を持つ。実行が確定した場合だけ `output` を付ける。状態は `rejected`、`unavailable`、`succeeded`、`failed`、`lost` である。待ち行列、自動の再試行、別のツールへの切り替えは行わない。

## 実行器

実行器は `probe(tool, location)` と `run(tool, location, request)` を持つ。実機への転送方法と利用者の操作状況を取得する方法は未定義であり、既存の資格やスクリプトを推測して使わない。

`probe` は `DesktopState` を返す。排他枠を確保した後で呼び、次の条件をすべて確かめる。

1. `available` が真である。
2. `owner_use` が偽である。利用者が操作中、または状態が不明なら始めない。登録情報の `owner_use` が偽でも、この実測を省かない。
3. `idle` が真である。実行中、または状態が不明なら始めない。
4. 依頼の区分をツールと実際の LLM 系統の両方が許す。

`DesktopState.model` は実行器が確かめた LLM 系統の `data_classes` と `tier` を持つ。呼び出し元の指定を使わない。区分が不明なら `public` のみとし、`sensitive` は共通判定の `standard` 以上も要する。依頼の区分は下げない。

`run` は処理の終了を待ち、`ExecutionResult` を返す。状態 `succeeded` または `failed` と結果の辞書を返す。まだ実行中の処理を成功として返してはならない。`NotImplementedError` は外部の操作を受け付ける前だけに使う。

接続先の同一性とデスクトップの状態を、実行器の側でも確かめる。秘密や個人情報の送信前検査、返す成果物の検査は制御 API と実行器の接続時に行う。この呼び出し口は資格を取得しない。

## 枠と結果不明時の扱い

同じデスクトップの枠を `flock` で排他的に確保する。満枠の呼び出しには直ちに `slot_busy` を返す。他のデスクトップは進む。

外部操作の前に、枠のファイルへ `uncertain` を書いて同期する。実行器の例外、終了が不明な返り値、制御プロセスの終了があっても、この印を残す。次の呼び出しは `reconciliation_required` で止まる。確定した成功と失敗では印を `idle` にする。ファイルは削除しない。

`reconcile(tool_id)` は信頼する接続処理だけが使う。枠を確保し、実行器がそのデスクトップの `available` と `idle` を確認できた場合だけ印を解除する。利用者が操作中なら、その後の新しい依頼も開始時の検査で拒否される。

## 時系列記録

事象 `tool.windows_call` はツール ID、状態、理由、区分だけを持つ。接続先の値、行き先の URL やアプリ、依頼本文、結果、例外の本文は書かない。記録に失敗し、outbox にも保存できない場合は外部操作を始めない。呼び出し口の `event_failures` に失敗の件数を残す。

## 検証と実環境での確認

担当の試験は `tests/b18b/winwork/` に置く。偽の実行器は `tests/fakes/b18b/winwork/` にある。ネットワークとサービスを使わず、実機を操作しない。

```sh
python3 -m pytest -p no:cacheprovider tests/b18b/winwork
```

| 項目 | 実環境で確かめる内容 |
|---|---|
| 接続先 | プロファイルの変数が、それぞれ別の意図したデスクトップを指す |
| 利用者の操作 | 操作中と不明の検知で新規開始が拒否される |
| LLM 系統 | 実際に操作する系統の区分と階層が、信頼できる取得元から返る |
| 実行の終了 | 返り値の時点で操作が終了している。結果不明なら再送しない |
| API と記録 | ツール専用の認証済み API から呼べる。時系列記録か outbox に残る |
| 環境・ワーカー | b14b1 の選び方とワーカーの候補に、能力を与えても Windows が出ない |

上の実環境の確認は未実行である。実機操作は禁止されており、ツール API と転送方法も未定義である。b14b1 の環境選択との結合は、その実装ができた後に別途確認する。

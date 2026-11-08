# 公開 Web の画面取得

`core/resources/screenshot/entry_expand.py` は S-25.26 の入口を扱う。公開ページの宣言を受け、引数と URL を検査する。ネットワークへの接続はドライバに任せる。この部品は環境の起動、枠の確保、提供元の選択を行わない。

## 選択側の接続

環境を選ぶ処理は入口の形を検査する段で `select_implementation(env, request, probe, implemented_tools)` を呼ぶ。`probe` には選ぶ直前の結果を渡す。`implemented_tools` はドライバが実装する共通の道具名の集合であり、能力の宣言から作らない。

`Choice.candidate_fields()` の返す `impl` または除外理由を、選択側が `env.selection` の候補に加える。`entry_unsupported` の候補を外した後の `parked`、満枠の `queued`、当日の利用時間による環境の並べ替えは、共通の選択側が扱う。

実装は台帳の `entrypoints.screenshot` の列順に選ぶ。標準の列は次のとおり。

```yaml
screenshot:
  - run:entries/screenshot
  - expand:config/sandbox-entry-steps.yaml#screenshot
```

`run` には `ax-runner` と `web-screenshot-full` の `ok` が必要である。`expand` には `web-screenshot` の `ok` と、`browser_navigate`・`screenshot` の実装が必要である。`requires` に `web-screenshot-full` があれば展開にしない。箱の名前では分岐しない。

## 実行側の接続

1. `prepare_entry` に同じ選択時点の環境、依頼、probe、道具名の集合を渡す。`scan_outbound` と `check_tool` は共通の sandbox policy に接続する。検査に失敗した場合は例外か `False` を返す。成功時は `None` または `True` を返す。
2. 戻った `Plan` を、そのジョブの記録とともに保持する。`Plan` は引数と steps の JSON の写しを持つ。後から台帳や元の依頼を書き換えても、そのジョブは変わらない。
3. `ScreenshotExecutor` に同じ検査関数、成果物の秘密検査、時計、待ち処理、タブ終了の関数を渡す。上限には S-17 の `sbx_shot_timeout_s`、`sbx_artifact_max_bytes`、`sbx_shot_max_height_px` を渡す。既定値は60秒、8388608バイト、16384pxである。
4. `execute(plan, driver=..., runner=...)` を呼ぶ。`expand` は `driver.call_tool(name, args, timeout_s)`、`run` は `RunnerPort` を使う。一度選んだ方式が失敗しても、別の方式で撮り直さない。
5. 戻った PNG とメタデータを `save_artifacts` に渡す。保存先は資源側が管理する成果物の根とジョブ ID で指定する。`<根>/<job_id>/page.png` と `meta.json` を新規作成し、大きさと sha256 の一覧を返す。同じジョブの保存先があれば失敗し、上書きしない。

`RunnerPort` はこの部品内の接続用インターフェースであり、箱の通信 API ではない。`run_entry(args, timeout_s)` を既存の runner client の start・status に接続する。戻り値は `out/page.png` と `out/meta.json` をキーにし、各値を `{size, sha256}` とする。`fetch(name, offset, size, timeout_s)` は、既存の fetch の base64 を復号したバイト列を返す。回収は12288バイト以下に分け、全体の残り時間を各呼び出しに渡す。b18a の runner client への接続は組み立て側が行う。

## 展開表とタブの終了

`config/sandbox-entry-steps.yaml` を `axcommon.schemas` で検査して読む。初期の表は移動、アダプタの待ち、画面取得の3手順だけを持つ。待ちは箱への呼び出しと steps の件数に数えない。`sandbox_steps_max` を超える展開は送信前に拒否する。依頼の引数で道具名や手順を差し替えられない。

`expand` は `full_page`、`width`、`height`、`headed` を使わず、`ignored_args` に記録する。幅と高さは PNG の IHDR から読む。`source` は環境 ID、`impl` は `expand`、`mode` は `viewport` となる。

`close_owned_tab(navigation_response, timeout_s)` は、その移動で開いたタブだけを、通常の許可表・秘密検査・事象記録を通して閉じる。利用者の既存のタブは閉じない。成功、画像の不備、時間切れの各経路で呼ぶ。時間切れの後も短い上限で後始末を試す。先に起きた失敗は後始末の失敗で置き換えない。

タブ ID の返し方と終了の引数は F-21.2 で実測待ちである。終了関数が無ければ `NotImplementedError` とし、ページを開く前に止める。試験の `fixture_close` と `owned_tab` は偽の部品だけの値であり、本番のドライバに転用しない。展開表の引数も実機の `tools/list` で照合してから接続する。

## URL と成果物の検査

URL は HTTP・HTTPS、ポート80・443だけを受ける。資格を含む URL、内部名、プライベート・ループバック・tailnet のアドレス、秘密を示すクエリ名を拒否する。省略・整数・16進表記の IPv4 も調べる。DNS の問い合わせはしない。公開の宣言を信頼する範囲と、ログインが必要なページを対象外とする範囲は S-25.26 に従う。

PNG の先頭8バイト、大きさ、IHDR、IHDR の CRC を検査する。`run` は回収前の記述とバイト列の大きさ・ハッシュも照合する。`meta.json` のモード、寸法、時刻、タイトル、HTTP 状態を検査し、アダプタが `source` と `impl` を加える。保存前に PNG とメタデータの両方を秘密検査へ渡す。

## 検証手順

担当の pytest は `tests/b18b/screenshot/` だけを実行する。偽の公開ページ、時計、runner、box の MCP を使う。外部通信とサービスの起動は必要ない。`tests/conftest.py` がソケット接続を拒否する。

| 試験 | この部品での確認 | 実環境での確認 |
|---|---|---|
| T-131 | 実機用の試験を skip する | 公開ページ、PNG、開いたタブの終了、probe の空き、1台、費用0、資格が残らないことを測る。通常の画面ジョブの作業ディレクトリの退避は sandbox 側で確かめる |
| T-138 | URL と引数、公開区分、PNG、分割回収、ハッシュ、メタデータ、上書き拒否を確かめる | Chromium と playwright-core による撮影と実際の回収時間を測る |
| T-148 | steps、待ち、許可検査、画像なし、PNG 不備、所有するタブだけの終了を確かめる | 道具の引数名とタブ終了の形を実機で照合する |
| T-185 | probe と実装の組み合わせ、全体取得の制限、方式の固定、Instabox の試験用実装、時間切れを確かめる | b18a の環境選択・枠・事象、Instabox の実ドライバと runner client を接続して確かめる |

実機への接続、scheduled-publisher の有効化、鍵の設定は、この成果物の作成中には行わない。

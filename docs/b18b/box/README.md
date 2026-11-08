# box ドライバ

`core/resources/box/box_mcp.py` は Simular box の MCP を使う AX の経路 J のドライバである。接続先と鍵の値はコードに置かない。

## 接続点

実行時は b18a の共通部を `BoxBindings` に渡す。

- `policy` は鍵の読込、許可表、引数検査、送信前と回収後の検査を持つ。
- `guard` は無料枠と箱の特定を判定する。
- `probe_common` は能力の判定を持つ。
- `runner` は `ax-sbx-run` の設置とジョブの記録を持つ。

この接続が無い機能は `IntegrationRequired` で止まる。別の環境や resources 全体は止めない。

## 安全の境界

ドライバは許可表外の道具、箱の作成・削除・更新・保存・復元を送らない。`billing` は `probe` 中の読み取りだけに限る。`box_wake` は `ensure_running` だけが呼ぶ。`computer_act` は共有の引数検査を通す。

`probe` は保存系を呼ばない。休止中の箱を起こさない。実行中だけ固定の段2を行う。固定の公開ページは `https://example.com` と `about:blank` だけである。

## 偽の MCP

`tests/fakes/box_fake.py` は通信を注入する callable である。ポートを待ち受けない。JSON と SSE、404 の再接続、401・402・403・429・5xx、休止、費用、台数、画像、切断、切れた出力を試験で再現する。

実機の MCP の未測定の引数名と応答形式は固定しない。`tools/list` の入力スキーマで確定できない起こしや状態取得の引数は `IntegrationRequired` にする。

## 確認

担当試験は `tests/b18b/box/` にある。偽の部品だけを使い、ネットワークとサービスを使わない。

実機の T-140、T-163、T-167、T-171 は go の後に別途実行する。

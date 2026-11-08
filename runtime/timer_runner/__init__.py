"""timer-runner コンテナの実行体（D-4.4、S-7.8、F-18.4）。`python -m runtime.timer_runner`。

kind=timer の登録済みプラグイン（定期ジョブ。例 hibi-state）を全部まとめて動かす 1 つのプラグイン。ax-work の網。
- 定期ジョブ: manifest の schedule で起こし、`plugins/timer/<id>/handler.py` の `async def run(ctx)` を呼ぶ。呼び出しの資格は、そのプラグインのトークン
  （/run/secrets/plugin-token-<id>）だけ。ほかのプラグインのトークンは使わない（F-18.4、T-184 の (f)）。
- 管理役が作る期限付きタイマー（S-7.8）: 制御 API の timers を読み、時刻になったら action を実行して run.state を書く。寿命（expires_at）を過ぎたら実行しない。
- mode: 自分も `draining` を持つ。run 以外では新しい起動をしない。予定に間に合わなければ missed を書き、追いかけない。
"""

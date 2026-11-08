"""サブループの実行系と timer-runner の実行体（B7。D-6、D-4.4）。

- runtime.subloop     1 コンテナ = 1 サブループの実行系（グラフ検査、executor、受け取り待ち、API）
- runtime.timer_runner  kind=timer のプラグインをまとめて動かす timer-runner コンテナの実行体
"""

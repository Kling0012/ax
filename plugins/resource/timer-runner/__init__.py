"""timer-runner（プラグインの timer の実行体）。worker-pool 用の timer は持たない。

worker-pool や resources の定期実行（週次確認、selector、supervisor、finisher）は
この部品の中でタイマーを回す。kind=timer のプラグインの登録は timer-runner の側で行う。
"""
VERSION = "0.1.0"
COMPONENT = "timer-runner"

"""資源台帳 resources（S-8.2、S-8.3、S-8.5、S-8.6、S-8.8、D-7、D-7.3、D-7.4、D-7.5）。

資源の状態（status、until、level、stats）はこの部品が持つ。選ぶのは worker-pool だが、
選ぶのに要る資源の読み取りと、worker-pool が頼む書き込み（outcome、exclude、prefix_cache）
を受け付けるのはここ。

口は 2 つ（F-18.4）: 中核の 8080（ax-core）と作業側の 8081（ax-work。宣言した API だけ）。
8081 の資源の口は manifest の宣言に合う呼び出しだけを通す（S-8 の POST と GET /v1/env/jobs*）。
8080 だけ資格で守る API は core-tokens（F-18.4、S-25.27）。
"""
VERSION = "0.1.0"
COMPONENT = "resources"

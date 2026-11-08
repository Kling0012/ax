"""オブザーバー（S-11.1〜S-11.4、D-10.1）。LLM を使わない。observe_s ごとに検査を回し、警報を出す。

検査の名前（health.alert の data.check）: liveness、watch、expired_pending、lag、starve、repeat、manager_trouble、drain_silent。
ほかに、切り替えの記録の取り込み（S-24.1 の 7。cutover.py）と、ホスト見張りのログの写し（hostlog.py）を同じ周期で行う。
拡張は ext/<束の ID>.py の register(app)。app.state.observer（Observer）に検査と予備役の呼び出しを足せる（docs/b8/observer.md）。
"""
VERSION = "0.1.0"
COMPONENT = "observer"

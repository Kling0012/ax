# B18b-4 統合

この副パッケージは既存の `resources` と `worker-pool` の拡張読み込みに `core/resources/ext/b18b.py` を登録する。新しい待受、経路、資格、提供元への接続は作らない。

## 差し込み口

`B18bAssembly` は、box のドライバ、screenshot の展開、Windows ツール、環境ワーカー、relay 登録をまとめる。資格、通信、probe、イベント、環境台帳、枠の確保は所有側の port として渡す。port が無い機能は `NotImplementedError` で止まり、他の機能には影響しない。

`EnvironmentSelection` は経路 J と経路 G の共通の選び方を持つ。経路 G は `worker-pool` の資格だけを `resources` の中核口で受ける。選ぶ API は枠を取らない。`record=1` の経路 G だけが依頼本文と秘密を含めない `env.selection` を残す。

`WorkerBridge` は親の worker-pool ジョブと子の環境ジョブを対応付ける。モデル枠と環境枠は、所有側の `reserve_pair` が一つの取引として確保する。失敗した片方の確保は戻す。隔離を確認できない環境、`irreversible: true`、`model_gateway` 以外の秘密は環境ワーカーへ出さない。環境の `verify` と報告の `STATUS` が両方合格したときだけ、親を `ok` にする。

`RelayRegistration` は `tunnel` を candidate として登録する。probe が成功したときだけ confirmed にする。`hq-public` と公開入口は変更しない。

## 確認

T-102、T-103、T-218 は `tests/b18b/integration/` の偽の所有者だけで確認する。ネットワーク、サービス、鍵、実在の箱は使わない。

T-103 と T-94 の実機部分は未実行である。中継先の現物、環境モジュールの隔離、提供元の鍵、箱の probe は構築後に確認する必要がある。現時点では b14b1 と b18a の所有 API が未接続のため、拡張は明示的な port の差し込みを待つ。

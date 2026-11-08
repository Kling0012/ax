# 故障注入 API

`tests/fakes/fault/` は HTTP の呼び出しをプロセス内で置き換える。ソケットと実サービスは使わない。T-8・T-12・T-14・T-17 でも使う共通部品で、B9-1 が所有する。

## FaultNet

`FaultNet(clock=fake_clock)` を作り、`add(name, backend, port=8080)` で ASGI の TestClient または Transport を登録する。`transport(caller="manager", zone="core")` の戻り値を `axcommon.client` のクライアントへ渡す。名前とポートは URL のサービス名・ポートに対応する。

| 操作 | 動作 |
|---|---|
| `stop(name, port=None, caller=None)` | 指定した先を接続不能にする |
| `start(name, port=None, caller=None)` | 指定した接続不能の故障を外す |
| `delay(name, seconds, path=None, method=None, port=None, times=None, caller=None)` | 偽時計を進める。要求の期限を超えたら `Unavailable` |
| `respond(name, status=503, json=None, path=None, method=None, port=None, times=None, caller=None)` | 本物へ届けず指定の応答を返す |
| `lose_response(name, times=1, path=None, method=None, port=None, caller=None)` | 本物へ届けた後に応答を失わせる |
| `clear(name=None)` | 注入した故障を外す。引数なしなら全件 |
| `apply_f184()` | F-18.4 の網と8080・8081の到達制限を有効にする |
| `calls` | caller・zone・service・port・method・path・outcome・status の呼び出し記録 |

`path` は fnmatch のパターンである。`times=None` は外すまで続く。`caller` を指定するとその呼び出し元だけに故障を注入する。到達制限はネットワークの接続の偽であり、API の資格と本文は接続先の実装が検査する。

```python
world.net.stop("timeline", caller="inspector")
insp.run_once()  # 点検係は自分の outbox に記録を溜める
world.net.start("timeline", caller="inspector")
insp.sink.flush()
world.net.respond("ledger", 503, times=2, path="/v1/todos")
world.net.delay("control", 30, times=1)
world.net.lose_response("control", times=1, method="POST")
```

## Chaos と独立した周回

`Chaos(clock=fake_clock)` はオブジェクトのメソッドへ故障を注入する。

| 操作 | 動作 |
|---|---|
| `raise_on(obj, name, exc=None, times=None)` | 例外を出す |
| `garbage(obj, name, value)` | 指定した値を返す |
| `slow(obj, name, seconds)` | 偽時計を進めてから本物を呼ぶ |
| `hang(obj, name)` | 同期なら `TimeoutError`、非同期なら待ち続ける |
| `replace(obj, name, fn)` | `fn(orig, *args, **kwargs)` で置き換える |
| `restore()` | メソッドを元へ戻す |

`await run_rounds({"pa": runner_a, "pb": runner_b}, n=3, timeout_s=5)` は、各部品を独立した非同期の周回として実行する。結果は名前ごとの `RoundStats` で、`ok`・`failed`・`timed_out`・`errors`・`rounds` を持つ。`timeout_s` は名前ごとの辞書でもよい。1部品の例外と時間切れは他へ伝わらない。`hang` の検査には必ず周回の期限を付ける。

## World とモデルの偽

`tests.fakes.b9.world.World` は本物の timeline・control・ledger を FaultNet に登録する。`world.manager()` と `world.inspector()` で管理役・点検係を作る。`world.add_plugin_runtime("pa")` は B7 の実行系を組み込む。

モデルの返答は B0 の `FakeModelGateway` に渡す。`world.pool.gw.script(reply(noop()))` のように使う。`world.pool` には `down()`・`up()`・`stuck_next()`・`reject_next(code)`・`lose_next()` があり、`jobs`・`bodies`・`cancelled` を読める。管理役はこの偽の worker-pool へ `role=manager` のジョブを出す。外部のモデルは呼ばない。

`world.emit(kind, subject, data, component=...)` は事象を書き、`world.events(kind)` は事象を返す。`tests/b9/helpers.py` の `reply`・`noop`・`in_min`・`seq_of`・`todos` も利用できる。共通 fixture の `_no_network` はソケットの接続を拒否する。

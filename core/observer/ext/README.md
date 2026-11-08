# core/observer/ext

他の束の拡張の置き場。`ext/<束の ID>.py` に `register(app)` を書く（axcommon.ext）。
`app.state.observer`（`core.observer.observer.Observer`）に対して次を使える。

- `add_check(name, fn)`: 検査を足す。`fn(ctx) -> list[Alert]`。
- `set_hook("backup", fn)`: 予備役（S-11.3）の呼び出し。`fn(alert)`。オブザーバーが先に `backup.activated` を書く。
- `set_hook("inspection", fn)`: 点検係への「再開」の指示（S-13.3）。
- `watch_signal = fn`: 各サブループの見張り用ノードの警報（`fn() -> list[{"target", "severity", "detail"}]`）。

`ext/b10a.py`（予備役、B10）は b10a が置く。この束は置かない。

"""行き先ハンドラの置き場（PLAN.json の conventions）。

`core/intake/dest/<pkg>/__init__.py` の register(app) を app.py の load_dest_packages が名前順に
読む。ハンドラは `app.state.intake.dest_handlers[行き先の名前] = handler` で登録する。
handler は `handler(app, dest(登録簿の行), body(指示), ident(受け口の身元)) -> (status, result)`。

b5 が登録するのは最初の 2 つ（`control.command`、`ledger.answer`）。ほかの行き先
（work.request、subloop.create など）は後続の束がこの下に自分のディレクトリを足す
（b10a、b12、b12b3、b16b が既定。他の束も同じ形で足せる）。
"""

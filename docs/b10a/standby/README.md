# 予備役の確認手順

管理役の不調を検出したオブザーバーが、予備役を1回呼ぶ。
根本原因の ToDo は起動ごとに1件作る。台帳に届かない間は保存し、復旧後に同じ ID で再配送する。
仕様は SPEC.md の S-11.3・S-11.4、DESIGN.md の D-10.2、受け入れ試験は T-14 に対応する。

## 接続

`core/observer/ext/b10a.py` の `register(app)` が `app.state.observer` に次の関数を登録する。

| 登録先 | 関数 | 動作 |
|---|---|---|
| `set_hook("backup", ...)` | `Standby.on_alert` | 警報を待ち行列へ積む。予備役は1件ずつ動く |
| `add_check("standby_drain", ...)` | `Standby.drain_check` | 台帳への再配送を行う。警報は返さない |

`backup.activated` と理由はオブザーバーが先に書く。予備役は管理役への呼び出し後に `manager.cycle` を書く。
呼び出しの失敗でも根本原因の ToDo を作る。

`ManagerModuleRunner` は次の呼び出しを要求する。

```python
core.manager.cycle.run_cycle(
    role="backup_manager",
    issued_by="backup",
    trigger={"check": ..., "target": ..., "detail": ...},
    emit_cycle=False,
)
```

これは既存実装が仮置きした接続で、管理役との合意は未完了だ。
現在の管理役には `Manager.run_cycle(self, trigger="tick")` があり、上のモジュール直下の関数はない。
関数がない場合は `NotImplementedError` となる。予備役は失敗を記録し、根本原因の ToDo を作る。
実装済みの管理役で予備役の周回が動くことは、まだ確認できていない。
同じ入力・同じ操作一覧と `issued_by=backup` の資格を使う接続は、管理役の担当との調整が要る。

## 頻度と保存先

頻度は `backup_window_days` の期間内の `backup.activated` を数える。
時系列記録に届かない間は、手元の起動記録で補う。同じ起動を二重に数えないよう、両方の件数の大きい方を使う。
`backup_limit` 回に達した起動から、根本原因の ToDo に `reason="backup:root_cause:top"` を付ける。
通常の根本原因の ToDo は `reason="backup:root_cause"` だ。
1回の起動で通常と最優先の2件は作らない。

利用者向けの知らせは `owner=user`、`state=todo`、`reason="backup:frequent_card"` の ToDo として保存する。
知らせは期間内に1件とし、その印を再起動後も引き継ぐ。
この知らせの HQ での表示と、既存の根本原因の ToDo を次の点検で最優先にする接続は未確認だ。
既存の管理役の点検処理は `backup.activated` から別の修繕 ToDo を先に作るが、この `reason` による既存 ToDo の選択は行っていない。
両方の接続と B10 完成時のモデル確認を、build/OPEN.md に残した。

保存先はオブザーバーの `data_dir/standby/` だ。

| 相対パス | 内容 |
|---|---|
| `activations.jsonl` | 手元の起動記録 |
| `state.json` | 利用者向けの知らせを出した時刻と件数 |
| `outbox/pending-todos.jsonl` | 再配送を待つ ToDo |
| `outbox/rejected-todos.jsonl` | 台帳が 4xx で拒否した ToDo |
| `outbox/.lock` | 再配送と追記を同時に行わないためのロック |

台帳が停止したら、未配送の ToDo を保存する。復旧後は順番に送る。
途中でまた届かなくなったら、残りを保存したままその回の再配送を終える。
台帳が同じ ID を 409 で返した場合は作成済みと扱う。
それ以外の 4xx は拒否した記録へ残し、繰り返し送らない。

## 手元の試験

リポジトリの直下から次を実行する。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/b10a/standby -q
```

試験は `tests/fakes/b10a/world.py` の台帳・時系列記録と、`fake_manager.py` の管理役を使う。
共通 fixture がソケットの接続を拒否する。サービスやポートは使わない。
今回の環境では pytest が標準の検索先にないため、既存の uv キャッシュの site-packages を `PYTHONPATH` に加えて実行した。導入は行っていない。

## 未実行の確認

| 確認 | 状態 | 理由 |
|---|---|---|
| T-14 の実管理役への接続と同じ入力・操作一覧 | 未実行 | 管理役の接続口が未提供。偽の関数との引数照合だけを確認した |
| 実制御 API が予備役の命令を `issued_by=backup` として受けること | 未実行 | 管理役との接続と専用の資格が必要。実機を使わない範囲のため |
| 管理役の復帰後に予備役の命令を引き継ぐこと | 未実行 | 管理役側の処理と実際の接続を要するため |
| 頻発時に既存の根本原因の ToDo を次の点検で最優先にすること | 未実行 | `reason` の印を点検処理へ渡す接続が未完了のため |
| 頻発の知らせの HQ 表示 | 未実行 | 台帳への保存だけを確認した。HQ 側との結合試験は担当外のため |
| コンテナの管理役を停止し、外のオブザーバーで予備役を動かすこと | 未実行 | 実機とサービスの起動・停止は禁止のため。B17 で確認する |
| 予備役を含む compose の検査とイメージへの管理役モジュールの同梱 | 未実行 | compose は b10a-2 の担当。イメージの構築は禁止のため |
| B10 完成時の予備役のモデルの利用者確認 | 未実行 | D-17 の確認事項。build/OPEN.md に記録した |

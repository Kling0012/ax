# 席の台帳と受け口の判定

`core/intake/seat/` は席の台帳を唯一更新する。台帳の形式は `{"seats": [...]}` とし、各行は S-6.8 の項目を持つ。`seats.yaml` の版は SQLite にも保存し、ファイルの権限、所有者、内容のダイジェストが最後に intake が保存した版と違うときは、その版を採用しない。

席の行は SSH の公開鍵と指紋、または tailnet の端末とトークンのダイジェストを持つ。トークンと登録コードの値は台帳、SQLite、事象、通知へ書かない。失効、期限切れ、停止では、対象の席の接続、承認待ち、命令、保持だけを差し込み口から外す。他の席、HQ、ローカルの入口は続ける。

`SeatService` は SSH の `ax-seat` uid、HQ の `AX_HQ_UID`、gate の固定アドレスと core token を受け口ごとに検査する。`axcommon.peercred.peer_uid(conn)` は Linux の `SO_PEERCRED` を接続ごとに読む。本文、ヘッダ、自己申告の値から身元や段を作らない。

## 受け口

`register(app, service, ...)` は b5 の `extra_listener_factories` に次を足す。

- `seat.sock` は `ssh` として扱う。`X-Ax-Seat` は接続元が `ax-seat` uid のときだけ照合に使う。
- `hq-intake/intake.sock` は `AX_HQ_UID` が数値のときだけ作る。uid が違う接続は受け付けない。
- `intake:8082` は `tailnet` の gate 用として作る。固定アドレスと core token の確認後にだけ席を照合する。

各受け口は別の factory で作る。待ち受け、台帳、保存先の障害は該当する受け口または運用の指示だけを止める。

## 未確定の wire

S-6.14 と D-5.3 は gate から intake へ渡す core token、Bearer、端末 ID、端末名のヘッダ名と、`POST /v1/seat/enroll` の JSON の形を定めていない。`decode_relay_evidence()` と登録交換の HTTP 処理は `NotImplementedError` 相当で拒否する。`SeatService.relay()` と `SeatService.enroll()` は、偽の部品から渡す `RelayEvidence` で台帳側の判定を確認できる。

SSH の `seat.add` の入力も未定である。HQ と承認の部品は、承認済みの値だけで `SeatStore.put()` または `SeatService.start_enrollment()` を呼ぶ必要がある。

## 実機で確認する項目

- `seat/` と `hq-intake/` の所有者、group、setgid、ソケットの権限
- コンテナから見た `SO_PEERCRED` の uid と `AX_HQ_UID` の一致
- gate を通した送信元 IP と固定アドレスの保持
- SSH の `authorized_keys` 更新と、失効時の既存プロセスの停止

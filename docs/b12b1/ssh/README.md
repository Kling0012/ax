# SSH の席とホスト用の雛形

この雛形は go の後に hub-host へ適用する。成果物の作成中は実機へ適用しない。

`host/ax-seat.sh` は `install`、`sync`、`revoke-all`、`status`、`add` を受け付ける。`install` は専用ユーザー、鍵の置き場、ゲートの複製、sshd の drop-in を準備する。sshd の設定は保留先へ置くだけで、検証と reload は行わない。

`sync` は intake が持つ `seats.yaml` と端末の対応表から `authorized_keys` を作る。`active` の SSH 席だけを入れ、端末名と `device_id` が対応表に合わない席は入れない。鍵の行は `restrict,pty,from=...,command="/usr/local/lib/ax-seat/ax --gate <seat_id>"` の固定形で作る。

`revoke-all` は `authorized_keys` を空にし、`ax-seat` のプロセスを止める。ほかのユーザーや席以外の AX のプロセスは対象にしない。

`add` は非常用の read 席だけを受け付ける。公開鍵と端末名を `host-ops.log` の記録と通知の経路へ渡す。`seats.yaml` を直接書き換えない。

SSH の強制コマンドは `SSH_ORIGINAL_COMMAND` をシェルへ渡さない。空の接続では `ax brief --short` と3行の案内を出す。コマンド付きの接続では、操作、段、標準入力、`ExposeAuthInfo` の公開鍵指紋を検査してから、席のソケットへ送る。`AX_SOCKET`、`AX_GATE_URL`、設定ファイルによる接続先の変更は使わない。

実環境でのみ確認することは、ユーザー作成、sshd の構文と実効値、drop-in の反映、実 SSH 接続、プロセス停止である。

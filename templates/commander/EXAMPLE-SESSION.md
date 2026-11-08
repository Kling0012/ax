# 司令塔の記録（参考のやりとり。作ったデータ）

この文書は、司令塔が AX に仕事を頼むときの典型的な流れを示す参考のやりとりです。登場する席の ID、ToDo、依頼 ID は作った物です。実在のホスト名、実在のアドレス、秘密は書いていません。

> 概要（3行）
> 司令塔は `ax brief` で再開し、`ax work` で仕事を頼み、終わったら `ax handoff` でメモを残します。承認が必要な操作は実行せず、承認待ちにします。席は `host-a` の例で読んでください。

## 目次

1. [準備](#1-準備)
2. [仕事を頼む](#2-仕事を頼む)
3. [承認待ち](#3-承認待ち)
4. [衝突](#4-衝突)
5. [開く](#5-開く)
6. [状態](#6-状態)

## 1. 準備

司令塔は最初に `ax brief --new-session` と `ax whoami` を実行します。

```bash
$ ssh ax-host-a ax brief --new-session
# guide_version がこのファイルの先頭の印と同じか確かめます。違う場合は ax guide を読み直します

$ ssh ax-host-a ax whoami
# 自分の席と段を確かめます
```

席の ID は `host-a` の例で読んでください。端末側の SSH 設定は次です。

```bash
$ ssh-keygen -t ed25519 -f ~/.ssh/ax_seat_host-a -C ax-seat:host-a
$ key=$(cat ~/.ssh/ax_seat_host-a.pub)  # 公開鍵だけを HQ に貼ります
```

対話は次です。

```bash
$ ssh -t ax-host-a
# 最初に ax brief --short（25 行以内）が出ます
```

## 2. 仕事を頼む

司令塔は `ax work` で AX に仕事を頼みます。長い作業は自分で回しません。

```bash
$ ssh ax-host-a ax work --project demo --title "索引の修正" --role impl
# {"ok": true, "op": "work", "request_id": "...", "say": "...", "data": {...}}
```

- `ax work` は `work.request` として `POST /v1/instructions` に送られます。同じ内容の再送は同じ `--request-id` を使います（書き込みの再送には同じ ID）。

進み具合は `ax events` と `ax todos` で確かめます。

```bash
$ ssh ax-host-a ax events --kind task.state --limit 20
$ ssh ax-host-a ax todos --project demo --state running
```

`ax status` の例です。

```bash
$ ssh ax-host-a ax status
# {"ok": true, "op": "status", "request_id": null, "say": "入口が応答した。", "data": {"control": {...}, "timeline": {...}}}
```

## 3. 承認待ち

戻しにくい操作（`core.change` など `irreversible` の行き先）は、実行せず承認待ちにします（HTTP 202、終了コード 4）。

```bash
$ ssh ax-host-a ax core change --spec - < spec.yaml; echo EXIT:$?
# EXIT:4
# {"ok": true, "op": "core", "request_id": "...", "data": {"approval_id": "..."}}
```

- 実行されていません。内容を利用者に伝えて待つか、利用者に HQ で解くよう頼みます。
- `ax approval wait <approval_id>` で状態を待ちます。1 回の待ちは `approval_wait_max_s`（既定 20 秒）までです。
- `ax approval withdraw <approval_id>` で取り下げます（依頼した席だけ）。

`ax answer` の例です（利用者の代わりに答えません。利用者が言った言葉をそのまま中継します）。

```bash
$ ssh ax-host-a ax answer todo-123 "確認しました" --quote '利用者の「確認しました」をそのまま渡します'
```

`user_only` のカードには答えられません（403、reason `user_only_card`）。

`ax control` の例です。

```bash
$ ssh ax-host-a ax control sl-demo run_now --reason "点検のため起動"
```

## 4. 衝突

別の司令塔が同じ対象を持っている場合、終了コード 8（衝突）で止まります。先の指示は変えません。持ち主と期限を利用者に伝え、待つか、利用者に HQ で解くよう頼みます。段が足りない（終了コード 6）場合は迂回しません。利用者に HQ で頼みます。

## 5. 開く

閉じるときは `ax handoff "次にすること・待っていること"` を残します。

```bash
$ ssh ax-host-a ax handoff "来週の再開: 今日の依頼の確認"
```

`ax decide` で判断の記録もできます。

```bash
$ ssh ax-host-a ax decide decision --title "索引の修正を先にする" --reason "利用者の希望に合わせる" --basis user_said --quote '利用者の言葉'
```

判断の記録は `brief` の「判断の記録」に出ます。

## 6. 状態

コードの現物で確かめました（未確認の部分は正直に書きます）。

- `ax brief` の実体（`GET /v1/read/brief`）は現時点で未実装です。ローカルでは `internal`（終了コード 1）で何も送りません（`host/lib/ax_cli/__init__.py` の `DEFERRED`）。
- `ax whoami`、`ax guide`、`ax show`、`ax approvals`、`ax approval show/wait/verify/export-go`、`ax seats`、`ax decisions`、`ax destinations` も同じく未実装です（`DEFERRED`）。
- `ax work`、`ax answer`、`ax control` は使える書き込みの操作です。`POST /v1/instructions` に送ります。
- `ax status`、`ax todos`、`ax events` は使える読み取りの操作です。`GET /v1/read/{status,todos,events}` です。
- `ax subloop create` と `ax core change` の `--spec` の送信形式は未確定です。file を渡しても `NotImplementedError` になります。
- 席の SSH の実機の接続（ホストの `sshd` と `gate`）は、切り替えと実機の確認（B17）が要ります。この参考のやりとりでは、SSH の形はコードとテストだけを使いました。

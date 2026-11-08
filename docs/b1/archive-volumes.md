# 退避の対象に加える方法

## ボリュームの archive/

F-20 のとおり、AX の各ボリュームの `archive/` を `archiver` に一律で渡す。`compose.d/b1.yaml` は b1 のボリューム（`timeline-data`、`memory-connector-data`、`archiver-data`）だけを書く。

ほかの束が自分のボリュームを加えるときは、自分の `compose.d/<束>.yaml` に次を足す。compose は同じサービスの `volumes` の一覧を束ねる。

```yaml
services:
  archive-init:
    volumes:
      - {type: volume, source: <サービス>-data, target: /v/<サービス>-data}
  archiver:
    volumes:
      - {type: volume, source: <サービス>-data, target: /vol/<サービス>-data, volume: {subpath: archive}}
  <サービス>:
    user: "${AX_UID}:${AX_GID}"
    depends_on:
      archive-init:
        condition: service_completed_successfully
```

- `archive-init` が `archive/` を作り、ボリュームと `archive/` の所有者を `AX_UID:AX_GID` に揃える。持ち主のサービスは、そのユーザーで動かす。
- 実機の判定で `subpath` が使えないと分かったら、全ボリューム一律で `<サービス>-archive` の独立したボリュームに替える（F-20）。替える作業は、判定の後に全束で行う。

## 退避元のホストのパス

`/src/<名>` への ro の bind は、`archive-sources.yaml` の一覧から作る。compose は一覧を回せないため、断片を生成する。

```text
python3 -m core.archiver.gen_sources_compose --sources ~/.config/ax/profile/archive-sources.yaml --out ${ax_state_dir}/deploy/archive-sources.compose.yaml
```

生成した断片は `host/ax-up.sh` が組み込む。起動のたびに同じコマンドで断片を `${ax_state_dir}/deploy/archive-sources.compose.yaml` へ再生成し、compose に `-f` で足す。`--sources` は profile のディレクトリの `archive-sources.yaml`（`AX_ARCHIVE_SOURCES_FILE` で変えられる）。

- 断片を再生成できなかったときは、その起動だけ断片を使わずに続ける（fail-soft）。archiver が退避元を mount できないことを警告に出す。
- 退避元の一覧が無いときも断片は渡さない（無い物は登録しない。F-10）。archiver が退避するのは AX のボリュームの `archive/` だけになる。

## 除去

- `archiver` は、ホストのパスの退避元の元を消さない。照合済みの manifest を `${ax_state_dir}/archive/verified/` に書く。
- 除去は `host/ax-archive-reap.sh` が行う。印 `${ax_state_dir}/archive/reap-enabled` がある間だけ動く。manifest の大きさ・mtime・inode が同じ物だけを取り除く。`keep: true` と退避元の `keep_original` に一致する物は残す。
- 印を作る・移すのは切り替えの実施者だけ。管理役・予備役・点検役のコンテナには `${ax_state_dir}/archive/` を渡さない。

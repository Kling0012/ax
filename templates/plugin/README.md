# プラグインテンプレート

このディレクトリには AX の実際のローダーに合わせた最小の雛形を置く。
コピー後は `example` を対象の ID や名前空間へ置き換え、束ごとの試験ディレクトリへ test stub を移す。
秘密の値はどの雛形にも書かず、secret 名だけを compose と manifest に宣言する。

## 種類ごとのファイル

| 種類 | 読まれる主なファイル | manifest | registry / compose |
|---|---|---|---|
| node | `plugins/nodes/<namespace>/<type>.py` | なし。Python の `schema` が入力 Schema | なし |
| resource | `plugins/resource/<id>/manifest.yaml` と実装 | あり | registry 断片と service の compose 断片 |
| timer | `plugins/timer/<id>/manifest.yaml` と `handler.py` | あり | registry 断片と `timer-runner` への secret 追加 |
| destination | config 断片、payload Schema、`core/intake/dest/<bundle>/` | なし | intake destinations の断片。個別 service は持たない |

`node` と `destination` の manifest、または node 用の registry 断片を作らない。現在の `plugin_kind` は `subloop`、`resource`、`timer` だけを受け付ける。

詳しい手順と検査は [プラグインガイド](../../docs/manual/plugins.md) を参照する。

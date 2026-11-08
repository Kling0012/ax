# B15 公開

B15 は公開待ち、製品登録簿、製品公開を実装する。公開と配備は環境 API の `env.run` を使う。製品固有の分岐は `config/products.yaml` に置き、既定値とグラフには製品名を入れない。

Dreamforge の `release-both` は `steam-workshop-upload` を要求する環境入口である。公開する SHA は入力の `target_sha` に固定する。公開の前後で SHA を照合し、公開後は prerelease と zip を確認する。失敗、保留、無効、対象外、成功を別に記録する。

環境入口が未定義の hibi は公開しない。`OPEN.md` の項目を解消してから、製品登録簿の入口を更新する。

担当試験は T-30、T-31、T-32、T-33、T-34、T-89、T-95 である。試験は偽の公開依存先だけを使う。

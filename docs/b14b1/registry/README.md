# 環境台帳

`config/environments.yaml` は実行環境の初期登録だけを持つ。状態、空き、利用量は資源台帳が持つ。接続先は profile の変数を参照し、資格はスロット名と置き場所だけを記す。値は書かない。

初日の5環境はすべて `trial` で登録する。`trial_for` が一致する公開の試行ジョブだけが選べる。通常の選択、負荷分散、`scope: each` は `trial` を候補に含めない。

選択は能力、実効区分、空き、費用、`rank`、当日の利用時間、登録順で候補を並べる。選択器は枠を取らない。実行を投入する処理が資源台帳の同じ取引で枠を確保する。

`cloud-sandbox` は probe が `ok` とした能力だけを使える。`private` と `sensitive` は渡さない。環境の `effective_data_classes` があるときは、台帳の `data_classes` との共通部分だけを使う。

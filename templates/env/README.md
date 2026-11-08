# templates/env — 実行環境の雛形

このフォルダは環境を管理下に入れるための雛形です。設計の正本は `docs/ENV-MODULE.md` です。

- `environment.template.yaml` — `manifest.yaml` の記入例（env-module の §4 の形）。複製して `<環境ID>.yaml` にします。
- `install-env-module.sh` — 環境モジュールを新しいホストに配置するための導入スクリプトの雛形（§11 の形）。`host/env-module-min/install.sh` を環境ごとの導入に合わせて変えた物を作成してください。ここにある雛形は引数と処理の順番の example です。
- `forced-command.authorized_keys.txt` — 強制コマンドの `authorized_keys` の 1 行の雛形（プレースホルダ）。足し方と権限を確認してください。

手元で手順を試したいときは、利用者向けの文書 `docs/manual/environments.md` を読んでください（ホストや箱を AX の管理下に入れる手順）。

## Status

- 環境の手順は [環境ガイド](../../docs/manual/environments.md) を参照してください。`templates/env-module/` は収録されていません。
- `templates/env/` は利用者向けの雛形です。これらの雛形の値は例です。実在の値は構築時に現物で確かめて記入します。
- B6 の最小版の導入は `host/env-module-min/install.sh` が実在します。その雛形の動作は `install.sh --check` で確かめられます。

`environment.template.yaml` の ID、接続方式、対応操作と権限を対象環境に合わせて確認してください。鍵やトークンの値は記入せず、導入先と秘密の保管場所を別に管理します。雛形を置くだけでは実行経路は有効になりません。

# B6a 資源台帳と worker-pool・runner

B6a は資源台帳（core/resources）と worker-pool（plugins/resource/worker-pool）を持つ。
選び方・quota・下位候補・能力評価・継続からの再開の判定などは S-8.4 から S-8.12 に従う。

## 作った物

- `core/resources/`: 資源台帳。SQLite で資源の状態を持つ（D-7.3、D-8）。
  - `catalog.py`: config/models.yaml の初期値の読み込み（S-8.2、F-7.3）
  - `store.py`: resources、exclusions、selections、discovery、weekly_checks の表
  - `core_auth.py`: core-tokens.sha256 のハッシュ照合（F-18.4、S-25.27）
  - `discovery.py`: ゲートウェイの /v1/models と登録簿の照合（S-8.2、R-N28、R-N29）
  - `jev.py`: Jev の呼び出し（S-8.4、F-7.4）
  - `promotion.py`: 段階の昇格・降格と無料モデルの除外（S-8.5）
  - `api.py`: 中核の口 8080 と作業側の口 8081（S-8.8）
  - `app.py`, `__main__.py`, `netbind.py`: 起動
- `plugins/resource/worker-pool/`: worker-pool。S-8.8 の API と選び方。
  - `selection.py`: 選び方（S-8.4）。Jev、GPT/Claude の 2 条件、quota、並び替え
  - `queueing.py`: 待機列と下位切替（S-8.4）
  - `launcher_client.py`: launcher の SSH クライアント（S-8.11、F-18.2）
  - `prompt.py`: 依頼の組み立てと prefix_id（S-8.12、D-7.5）
  - `resume.py`: 継続からの再開の判定（S-8.12、a〜d）
  - `usage.py`: 使用量の記録（S-8.12、T-111）
  - `probe.py`: prefix_cache の計測（S-8.12、T-113）
  - `exclusivity.py`: 排他（S-8.12、T-114）
  - `children.py`: 子エージェント（S-8.9、T-47）
  - `supervise.py`: 監視と STATUS の読み取り（S-8.6、D-7.1）
  - `jobs.py`: jobs と attempts の表（D-7.5）
  - `api.py`, `app.py`, `__main__.py`, `netbind.py`
- `config/roles.yaml`: 役割ごとの最低段階と同額時の既定候補の順（S-8.3）
- `config/models.example.yaml`: モデルの登録簿の見本（F-7.3）
- `config/runners/schema-only/agent-cli.yaml`: runner の形の見本（F-7.2）
- `schemas/ext/b6a/job-launcher.schema.json`: job-start の依頼の形（F-18.2）
- `compose.d/b6a.yaml`: resources と worker-pool のサービス定義
- `plugins/registry.d/b6a.yaml`: 登録の断片
- `plugins/resource/worker-pool/manifest.yaml`: worker-pool の宣言（S-4.2）
- `plugins/resource/resources/manifest.yaml`: resources の宣言（S-4.2）

## 差し込み口

- `core/resources/ext/b6a.py` と `plugins/resource/worker-pool/ext/b6a.py` が
  `<部品>/ext/<束の ID>.py` の register(app) を持つ（axcommon.ext.load_extensions）。
  b14b1、b18a、b18b が core/resources/ext/b14b1.py と ext/b18a.py と ext/b18b.py を足す。
- core/resources/envs/、sandbox/、box/、screenshot/、winwork/ は空の区切りである。
  b14b1 が envs/、b18a が sandbox/、b18b が box/ と screenshot/ と winwork/ を実装する。

## 試験

- `tests/b6a/`: T-10、T-41〜T-47、T-109〜T-114
- `tests/fakes/b6a/`: launcher_fake.py、jev_fake.py、model_gateway.py

## 決めた細部

細部は build/DECISIONS.md の b6a の項目に書く。

## 未実行

実環境でしか通せない試験（実機の launcher、ゲートウェイ、Jev）は build/status/b6a.md に残す。

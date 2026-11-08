# B9 の検証手順

作業場所は配備用リポジトリの根とする。`TMPDIR=<temp-dir>` を使う。以下の検証はネットワーク・サービス・Docker デーモンを使わない。Docker の起動・ビルドと実際の移行は別の実施段階で行う。

## 担当する試験

| 試験 | 所有者 | 確認する内容 |
|---|---|---|
| T-13 `tests/b9/test_t13_manager.py` | B9-1 | 管理役の周回と権限 |
| T-16 `tests/b9/test_t16_inspection.py` | B9-2 | 点検の予定・draining・長い作業の継続・沈黙時の解除・点検外で解除しないこと・管理役との往復・再起動・送信待ち |
| T-17 `tests/b9/test_t17_independence.py` | B9-1 | 故障した部品から他の周回への影響 |

B9-2 の確認は T-16 だけを実行する。T-13 と T-17 はそれぞれの担当が実行する。

```sh
python3 -m py_compile core/inspector/*.py tests/b9/test_t16_inspection.py
python3 -m pytest -p no:cacheprovider tests/b9/test_t16_inspection.py -q

docker compose --project-directory "$PWD" \
  --env-file tools/ci-fixtures/ci.env \
  -f compose.yaml -f compose.d/b1.yaml -f compose.d/b9.yaml config --quiet
```

`b1.yaml` は共通の `pii-list` と `archive-init`・`archiver` の定義を含む。`b9.yaml` はそこへ2つのボリュームを追加する。Compose の構成確認はサービスの起動と API の疎通を確認する試験ではない。

## 依存キャッシュを使う場合

この構築環境のシステム Python には pytest がない。既存の uv キャッシュを `PYTHONPATH` に加えて実行できる。導入とダウンロードは行わない。キャッシュの場所と Python の ABI を照合し、`rpds` は実行する Python と一致する物を先頭に置く。実行前に以下の変数を、確認済みのローカルパスへ設定する。

```sh
# AX_TEST_CACHE: uv の archive-v0 の場所
# AX_TEST_RPDS: Python と ABI が一致する rpds の親ディレクトリ
AX_TEST_DEPS=$(printf '%s:' "$AX_TEST_CACHE"/*)
PYTHONPATH="$AX_TEST_RPDS:$AX_TEST_DEPS" \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python3 -m pytest -p no:cacheprovider tests/b9/test_t16_inspection.py -q
```

この試験には pytest・jsonschema・PyYAML・Starlette・httpx とその依存が要る。依存がないための skip を合格として扱わない。プラグインの自動読込を止めた場合、プロジェクトの `asyncio_mode` に警告が出ることがある。T-16 は `asyncio.run` で非同期処理を実行する。

## 実環境での確認

以下は実環境でだけ確認できる。構築作業では実行しない。

| 対象 | 実施する段階 | 合格条件 |
|---|---|---|
| 管理役の資格 | 移行前のホスト側準備 | `core-tokens/manager` と受信側のハッシュ登録があり、台帳が管理役からの書き込みを受け付ける |
| 名前つきボリューム | 移行時 | `archive-init` の所有者設定後、両サービスが自分の `/data` にだけ保存できる |
| 網と待受 | 配置後 | 両サービスが `ax-core` だけに所属し、ホストへ公開するポートがない。必要な中核の8080へ届く |
| 生存と再起動 | 配置後 | 両方の `/healthz` が通り、再起動しても購読位置・点検予定・進行中の点検が残る |
| archive subpath | F-20 の実機判定時 | `archiver` は各 `archive/` だけを開き、本体のデータベースを開かない |
| 管理役の選択 | worker-pool との統合時 | draining 中でも点検に必要な管理役の判断が進む。モデル選択の設定変更は提案に留まる |
| 点検中の沈黙 | 統合試験時 | オブザーバーの警報が点検係へ届き、点検係の命令だけが解除される |
| HQ の利用者変更 | B10 との統合時 | 利用者による変更の前後と理由が `inspection.phase=review` に残る |

秘密の作成・実機の起動・資格の疎通・モデルへの接続は、このオフライン検証の対象外である。

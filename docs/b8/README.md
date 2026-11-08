# B8 オブザーバーとホスト見張り

対応する段: B8。仕様: S-11.1〜S-11.6、S-16.7、S-24.1。設計: D-10、D-10.1、D-10.3。現物と既定: F-5.5、F-14.2、F-18.3、F-18.6。

## 構成

| 物 | 置き場 | 内容 |
|---|---|---|
| オブザーバー | `core/observer/` | LLM を使わない検査係。`observe_s` ごとに検査を回し、警報を出す。[observer.md](observer.md) |
| ホスト見張り | `host/ax-hostwatch.sh`、`.service`、`.timer`、`host/lib/hostwatch/` | コンテナの外で動く短い周期。`sudo -n` で起動し直しと配備を行う。[hostwatch.md](hostwatch.md) |
| 要求ファイル | `core/observer/hostreq.py`、`schemas/ext/b8/host-request.schema.json` | コンテナからホスト見張りへの頼み方。[host-requests.md](host-requests.md) |
| 配備 | `host/ax-deploy.sh`、`host/lib/hostwatch/deploy.py` | 受け入れの印のある main を取り込み、ビルドし、起動し、戻す |
| ホストの3本の仕上げ | `host/ax-env.sh`、`host/ax-up.sh`、`host/ax-redact-index.sh` | B0 の最小版に F-18.6 の B8 の列を足した。[host-scripts.md](host-scripts.md) |
| 切り替えの記録 | `tools/cutover_note.py`、`core/observer/cutover.py` | 追記の道具と、オブザーバーの取り込み。[cutover.md](cutover.md) |
| compose | `compose.d/b8.yaml` | `observer` のサービス |
| 実環境の確認 | [real-env-checks.md](real-env-checks.md) | この環境では通せない試験 |

## 試験

`tests/b8/`（偽の部品は `tests/fakes/b8/`）。ネットワークとサービスを使わない。sudo と docker は偽の実行器・偽の sudo に差し替える。

- T-12: `test_t12_observer.py`
- T-104: `test_t104_hostwatch.py`、`test_t104_deploy.py`、`test_t104_observer_side.py`、`test_hostwatch_script.py`、`test_compose_b8.py`、`test_host_request_contract.py`
- T-120（B8 の仕上げ）: `test_t120_finish.py`。B0 の `tests/b0/test_t120_host_scripts.py` も同じ結果で再実行する
- T-150: `test_t150_cutover.py`

## b8-1 の検証範囲

b8-1 は観測、要求ファイルの書き手、ホストログの取り込み、切り替え記録を受け持つ。ホスト見張りと配備の実行は b8-2 が受け持つ。
オフラインの受け入れは `test_t12_observer.py`、`test_t104_observer_side.py`、`test_host_request_contract.py`、`test_t150_cutover.py`、`test_compose_b8.py` の5ファイルで行う。
compose の局所検査は `compose.yaml`、`compose.d/b1.yaml`、`compose.d/b8.yaml` を `tools/ci-fixtures/ci.env` の値で解決する。全パッケージを束ねた検査と実機試験は別に行う。

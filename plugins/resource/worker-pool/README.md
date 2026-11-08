# worker-pool

ワーカーのジョブを受け付け、モデルを選んで実行します。

## 設定と運用

manifest の `schedule` は `{"kind": "interval", "every_s": 1, "missed": "record_only"}` です。 `missed: record_only` は取りこぼしの記録を指定します。

| 設定 | 既定値 | 説明 |
|---|---|---|
| `role_candidates` | `[]` | 役割ごとの同額時の既定候補の順（S-8.3）。適格候補を制限しない |
| `family_cap` | `""` | 系統ごとの同時実行上限（S-8.7） |
| `model_cap` | `""` | モデルごとの同時実行上限（S-8.7） |
| `jev_enabled` | `true` | Jev がモデル説明と作業の適合を判定するかを決める（F-7.4） |
| `lower_fallback_enabled` | `true` | 下位候補への切替を許可するか（S-8.4） |
| `lower_fallback_wait_s` | `300` | 下位候補への切り替え前に待つ秒数（S-8.4） |
| `model_descriptions` | `""` | 登録モデルの用途・能力と難しい作業向けの説明（S-8.3） |
| `promotion_passes` | `3` | 昇格に要る連続合格の回数（S-8.5） |
| `demotion_failures` | `2` | 降格に要る連続不合格の回数（S-8.5） |
| `free_success_threshold` | `0.7` | 無料モデルの成功率の下限（S-8.5） |
| `free_minimum_runs` | `3` | 成功率を判定する最低試行数（S-8.5） |
| `free_recent_failure_min` | `60` | 直近の能力上の失敗で通常候補から外す期間（分）（S-8.5） |

manifest の秘密の識別子: `model-gateway-key`、`jev-key`、`launcher-ssh-key`。これらは環境変数名ではありません。

Compose のサービス `worker-pool` として起動します。停止・再開は配備の Compose 構成で行います。依存サービスへの影響を確認してください。

モデルゲートウェイや launcher への通信があります。実行はジョブと配備に依存します。

試験: `tests/b6a/`。

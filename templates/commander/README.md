# 司令塔の雛形（commander templates）

司令塔のエージェントに使う雛形は、このフォルダに集まります。席の作り方と司令塔の接続の詳細は、`docs/manual/cli-reference.md` の §4〜§7 と `docs/FACTS.md` の F-9.2〜F-9.4 を参照してください。席と司令塔の安全は `docs/manual/security.md` です。

## 目次

- [ENTRANCE.md](ENTRANCE.md) — エージェントに司令塔の指示ファイルを置くための雛形。入口の書式は `docs/FACTS.md` の F-5.3、生成物は `commander/targets.yaml` と `tools/gen_commander_guide.py` に従います。
- [SEAT-REGISTRATION.md](SEAT-REGISTRATION.md) — 別の端末から `ax` で司令塔として使う席を作る手順。HQ が使えないときの非常用も書きます。
- [EXAMPLE-SESSION.md](EXAMPLE-SESSION.md) — 司令塔が AX に仕事を頼むときの参考のやりとり。作ったデータで書いています。

## 使い方

- 生成物（`tools/gen_commander_guide.py` が `dist/commander/` に作ります）を `ENTRANCE.md` の §3 のとおり取り出して置きます。生成物は手で直さず、正本（`commander/GUIDE.md`）を直して生成し直します。
- 席の登録は HQ の「司令塔の接続」画面で行います（`SEAT-REGISTRATION.md` の §3）。HQ が使えないときは `host/ax-seat.sh add`（段 `read` のみ）を使います。
- `ax` コマンドの使い方と終了コードは `docs/manual/cli-reference.md` を参照してください。

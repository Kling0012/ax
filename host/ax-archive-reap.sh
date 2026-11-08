#!/usr/bin/env bash
# 退避の元の除去（reap）。ホスト見張りの周期で operator が動かす（S-14.3、F-20、D-13）。
#
# 何をするか:
#   ${ax_state_dir}/archive/verified/<name>/<日付>.json（archiver が、退避先での内容の照合に成功した物だけを書く manifest）の各ファイルについて、
#   元の場所のファイルが「大きさ・更新時刻（ナノ秒）・inode とも manifest と同じ」ことを再確認し、同じ物だけを取り除く。
#   違う物（照合の後で書き換えられた）、manifest が keep: true の物、退避元の keep_original に一致する物は残す。
# 動く条件:
#   ${ax_state_dir}/archive/reap-enabled があるとき。無ければ何もしない。確認済みのファイル 1 件ごとに印をもう一度見るので、
#   印が移された（止められた）時点で、確認前の物は除去されない（T-154）。
#   印を作る・移すのは切り替えの実施者だけ（F-20 の「有効化」と「reap の停止と戻し方」）。有効化は b17。このスクリプトは印を作らない。
#   退避元の remove_original が true の物だけが対象（archive-sources.yaml を読み直す。manifest だけでは決めない）。
# 記録: ${ax_state_dir}/archive/reap.log（1 行 1 JSON）。manifest ごとの処理済みの印は <日付>.reap.json（同じ場所）。
# 使い方: host/ax-archive-reap.sh [--state-dir DIR] [--sources FILE] [--max-files N] [--dry-run]
#   既定: --state-dir は $AX_STATE_DIR か ~/.local/state/ax、--sources は ~/.config/ax/profile/archive-sources.yaml
# 終了コード: 0 正常（印が無くて何もしない場合も 0）/ 1 引数や設定の誤り
set -euo pipefail

state_dir="${AX_STATE_DIR:-$HOME/.local/state/ax}"
sources_file="${AX_ARCHIVE_SOURCES_FILE:-$HOME/.config/ax/profile/archive-sources.yaml}"
max_files=1000
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --state-dir) state_dir="${2:?--state-dir に値が要る}"; shift 2 ;;
    --sources) sources_file="${2:?--sources に値が要る}"; shift 2 ;;
    --max-files) max_files="${2:?--max-files に値が要る}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "ax-archive-reap: 知らない引数: $1" >&2; exit 1 ;;
  esac
done
case "$state_dir" in /*) ;; *) echo "ax-archive-reap: --state-dir は絶対パス" >&2; exit 1 ;; esac
case "$max_files" in ''|*[!0-9]*) echo "ax-archive-reap: --max-files は整数" >&2; exit 1 ;; esac

marker="$state_dir/archive/reap-enabled"
if [ ! -e "$marker" ]; then
  echo "ax-archive-reap: reap-enabled が無い。何もしない"
  exit 0
fi

mkdir -p "$state_dir/locks"
exec 9>"$state_dir/locks/archive-reap.lock"
if ! flock -n 9; then
  echo "ax-archive-reap: 別の周期が動いている。何もしない"
  exit 0
fi

export AX_REAP_STATE_DIR="$state_dir" AX_REAP_SOURCES="$sources_file" AX_REAP_MAX="$max_files" AX_REAP_DRY="$dry_run"
exec python3 -I - <<'PY'
import fnmatch
import json
import os
import stat
import sys
import time
from pathlib import Path

state = Path(os.environ["AX_REAP_STATE_DIR"])
marker = state / "archive" / "reap-enabled"
verified = state / "archive" / "verified"
log_path = state / "archive" / "reap.log"
max_files = int(os.environ["AX_REAP_MAX"])
dry = os.environ["AX_REAP_DRY"] == "1"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_sources(path):
    try:
        import yaml
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"ax-archive-reap: 退避元の一覧を読めない: {type(e).__name__}", file=sys.stderr)
        return None
    items = doc.get("sources") if isinstance(doc, dict) else doc
    out = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("name") and it.get("path"):
            out[str(it["name"])] = {
                "root": os.path.realpath(os.path.expanduser(str(it["path"]))),
                "remove": bool(it.get("remove_original", False)),
                "keep": [str(x) for x in (it.get("keep_original") or [])],
            }
    return out


def matches(rel, patterns):
    for pat in patterns:
        p = pat.replace("**", "*")
        if fnmatch.fnmatchcase(rel, p) or (p.startswith("*/") and fnmatch.fnmatchcase(rel, p[2:])):
            return True
    return False


def atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


sources = load_sources(os.environ["AX_REAP_SOURCES"])
if sources is None:
    sys.exit(1)

summary = {"at": now(), "removed": 0, "changed": 0, "missing": 0, "keep": 0, "skipped_sources": [], "stopped": False, "dry_run": dry}
budget = max_files
if verified.is_dir():
    for sdir in sorted(p for p in verified.iterdir() if p.is_dir()):
        src = sources.get(sdir.name)
        if not src or not src["remove"]:
            summary["skipped_sources"].append(sdir.name)
            continue
        root = src["root"]
        for mpath in sorted(sdir.glob("*.json")):
            if mpath.name.endswith(".reap.json"):
                continue
            if budget <= 0 or summary["stopped"]:
                break
            try:
                manifest = json.loads(mpath.read_text(encoding="utf-8"))
                files = manifest["files"]
            except (OSError, ValueError, KeyError):
                continue
            side = mpath.with_name(mpath.stem + ".reap.json")
            try:
                done = json.loads(side.read_text(encoding="utf-8")).get("done", {})
            except (OSError, ValueError):
                done = {}
            changed_side = False
            for ent in files:
                rel = ent.get("path", "")
                if rel in done:
                    continue
                if budget <= 0:
                    break
                if not os.path.lexists(marker):
                    summary["stopped"] = True  # 止められた。確認前の物は除去しない（T-154 の (d)）
                    break
                if ent.get("keep") or matches(rel, src["keep"]):
                    done[rel] = "keep"; summary["keep"] += 1; changed_side = True
                    continue
                target = os.path.normpath(os.path.join(root, rel))
                if os.path.isabs(rel) or not (target == root or target.startswith(root + os.sep)) or rel.startswith(".."):
                    done[rel] = "outside"; changed_side = True
                    continue
                try:
                    st = os.lstat(target)
                except FileNotFoundError:
                    done[rel] = "missing"; summary["missing"] += 1; changed_side = True
                    continue
                except OSError:
                    continue
                if (not stat.S_ISREG(st.st_mode) or st.st_size != ent.get("size") or st.st_mtime_ns != ent.get("mtime_ns")
                        or st.st_ino != ent.get("inode")):
                    done[rel] = "changed"; summary["changed"] += 1; changed_side = True
                    continue
                budget -= 1
                if dry:
                    summary["removed"] += 1
                    continue
                try:
                    os.unlink(target)  # 照合済みで manifest と同じ物だけを取り除く（退避の中の移動の一部）
                except OSError:
                    continue
                done[rel] = "removed"; summary["removed"] += 1; changed_side = True
            if changed_side and not dry:
                atomic_write(side, json.dumps({"manifest": mpath.name, "updated_at": now(), "done": done}, ensure_ascii=False, indent=1) + "\n")
if not dry:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
print(json.dumps(summary, ensure_ascii=False))
PY

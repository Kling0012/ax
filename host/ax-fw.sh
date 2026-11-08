#!/usr/bin/env bash
# AX の firewall の規則を作って反映する（F-18.1、T-124）。B6 が作る。冪等。
#
# 使い方: host/ax-fw.sh <コマンド> [--profile FILE] [--after-rules FILE] [--archive-dir DIR] [--no-reload]
#   rules       DOCKER-USER の規則のブロック（# BEGIN AX … # END AX）を標準出力に出す。何も書かない。
#   ufw-rules   ufw の許可規則（ufw allow …）の一覧を標準出力に出す。実行しない。
#   check       after.rules の AX のブロックが、今の profile から作る物と一致するか（0 一致 / 1 違う・無い）。何も書かない。
#   apply       after.rules の AX のブロックを書き換え、ufw reload する。内容が同じなら何もしない（控えも reload も無い）。
#   ufw-apply   ufw-rules の規則を ufw に入れる（ufw allow は同じ規則を足し直さない）。入れる前に ufw status numbered を archive に保存する。
# 規則の形（F-18.1）: 環境ごとに `-i ax-work -s <resources> -o tailscale0 -d <環境> -p tcp --dport <port> -j ACCEPT`、退避コネクタ archiver の例外を 1 本
#   （`-i ax-core -s <archiver> … -d <archive_dest>`）、続けて ax-core・ax-work・ax-job から tailscale0 への転送を DROP、最後に RETURN。
# 環境の宛先は profile の env_ssh_targets（{id, host, port}）の host を `tailscale ip -4 <host>` で解決する。archive_dest も同じ。
#   アドレスはこのスクリプトにも git にも書かない（実行のときに解決して after.rules にだけ入る）。1 件でも解決できなければ何も書かず終了コード 1。
# 書き換えの前に、after.rules を archive へ cp -p し、`iptables -S DOCKER-USER` の出力を archive へ保存する（削除しない）。
# 環境変数（試験用の差し替え）: AX_FW_SUDO（既定 "sudo -n"）、AX_FW_TAILSCALE、AX_FW_IPTABLES、AX_FW_UFW、AX_FW_OWNER（root:root）、
#   AX_FW_RESOURCES_IP、AX_FW_ARCHIVER_IP、AX_FW_WORKER_POOL_IP、
#   AX_PROFILE_FILE（--profile を渡さない場合の profile。既定 ~/.config/ax/profile/default.yaml）
# 終了コード: 0 成功 / 1 失敗（何も書いていない。ただし書き換えの後の確認の失敗は 3）/ 2 使い方の誤り / 3 反映後の確認に失敗
# 実際の ufw の反映と実機の確認（T-124）は go の後に operator が実行する。このスクリプトの試験は偽の tailscale・ufw・iptables で行う。
set -euo pipefail

PROFILE="${AX_PROFILE_FILE:-${HOME}/.config/ax/profile/default.yaml}"
AFTER_RULES="/etc/ufw/after.rules"
ARCHIVE_DIR=""
RELOAD=1
CMD=""

SUDO="${AX_FW_SUDO-sudo -n}"
TAILSCALE="${AX_FW_TAILSCALE:-tailscale}"
IPTABLES="${AX_FW_IPTABLES:-iptables}"
UFW="${AX_FW_UFW:-ufw}"
OWNER="${AX_FW_OWNER:-root:root}"
# 固定の内側のアドレス（F-18.1。compose の ipv4_address と同じ。実在のホストのアドレスではない）
RESOURCES_IP="${AX_FW_RESOURCES_IP:-}"
ARCHIVER_IP="${AX_FW_ARCHIVER_IP:-}"
WORKER_POOL_IP="${AX_FW_WORKER_POOL_IP:-}"
# Read only the three address assignments; never execute the configuration file.
fw_env_file="${AX_HOME:-$HOME/.local/share/ax/prod}/config/launcher.env"
if [ -r "$fw_env_file" ]; then
  while IFS='=' read -r fw_key fw_value || [ -n "$fw_key" ]; do
    case "$fw_key" in
      AX_FW_RESOURCES_IP) [ -n "$RESOURCES_IP" ] || RESOURCES_IP="$fw_value" ;;
      AX_FW_ARCHIVER_IP) [ -n "$ARCHIVER_IP" ] || ARCHIVER_IP="$fw_value" ;;
      AX_FW_WORKER_POOL_IP) [ -n "$WORKER_POOL_IP" ] || WORKER_POOL_IP="$fw_value" ;;
    esac
  done < "$fw_env_file"
fi
BEGIN_MARK="# BEGIN AX"
END_MARK="# END AX"

die() { echo "ax-fw: $*" >&2; exit 1; }
usage() { sed -n '2,19p' "$0"; }

[ "$#" -ge 1 ] || { usage >&2; exit 2; }
CMD="$1"; shift
while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) [ "$#" -ge 2 ] || { echo "ax-fw: --profile に値が要る" >&2; exit 2; }; PROFILE="$2"; shift 2 ;;
    --after-rules) [ "$#" -ge 2 ] || { echo "ax-fw: --after-rules に値が要る" >&2; exit 2; }; AFTER_RULES="$2"; shift 2 ;;
    --archive-dir) [ "$#" -ge 2 ] || { echo "ax-fw: --archive-dir に値が要る" >&2; exit 2; }; ARCHIVE_DIR="$2"; shift 2 ;;
    --no-reload) RELOAD=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ax-fw: 知らない引数: $1" >&2; exit 2 ;;
  esac
done
case "$CMD" in rules|ufw-rules|check|apply|ufw-apply) ;; -h|--help) usage; exit 0 ;; *) echo "ax-fw: 知らないコマンド: $CMD" >&2; usage >&2; exit 2 ;; esac

# A missing or invalid address must never produce rules using placeholder values.
if ! python3 - "$RESOURCES_IP" "$ARCHIVER_IP" "$WORKER_POOL_IP" <<'PYCODE'
import ipaddress
import sys
try:
    for value in sys.argv[1:]:
        ipaddress.IPv4Address(value)
except ValueError:
    sys.exit(1)
PYCODE
then
  die "AX_FW_RESOURCES_IP, AX_FW_ARCHIVER_IP, AX_FW_WORKER_POOL_IP: missing or invalid configuration"
fi

# $SUDO は空でもよい（試験）。語に分けて使う。
# shellcheck disable=SC2206
SUDO_ARR=($SUDO)
# shellcheck disable=SC2206
TS_ARR=($TAILSCALE)
# shellcheck disable=SC2206
IPT_ARR=($IPTABLES)
# shellcheck disable=SC2206
UFW_ARR=($UFW)
run_root() { if [ "${#SUDO_ARR[@]}" -gt 0 ]; then "${SUDO_ARR[@]}" "$@"; else "$@"; fi; }

# --- profile の読み取り（python3 -P。標準ライブラリと PyYAML） ---
read_profile() {
  python3 -P - "$PROFILE" <<'PY'
import re, sys
try:
    import yaml
except ImportError:
    print("E\tPyYAML が無い"); sys.exit(0)
try:
    p = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
except OSError as e:
    print("E\tprofile が読めない: " + e.strerror); sys.exit(0)
except yaml.YAMLError:
    print("E\tprofile が YAML でない"); sys.exit(0)
if not isinstance(p, dict):
    print("E\tprofile の最上位がマッピングでない"); sys.exit(0)
host_re = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
id_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
def port_ok(v): return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535
targets = p.get("env_ssh_targets")
if targets is None: targets = []
if not isinstance(targets, list):
    print("E\tenv_ssh_targets が配列でない"); sys.exit(0)
seen = set()
for t in targets:
    if not (isinstance(t, dict) and id_re.match(str(t.get("id", ""))) and host_re.match(str(t.get("host", ""))) and port_ok(t.get("port"))):
        print("E\tenv_ssh_targets の項目が不正（id・host・port）"); sys.exit(0)
    if t["id"] in seen:
        print("E\tenv_ssh_targets の id が重複: " + t["id"]); sys.exit(0)
    seen.add(t["id"])
    print("T\t%s\t%s\t%d" % (t["id"], t["host"], t["port"]))
a = p.get("archive_dest")
if a is not None:
    if not (isinstance(a, dict) and host_re.match(str(a.get("host", ""))) and port_ok(a.get("port"))):
        print("E\tarchive_dest が不正（host・port）"); sys.exit(0)
    print("A\t%s\t%d" % (a["host"], a["port"]))
PY
}

# tailscale ip -4 <host> -> 最初の IPv4
resolve_ip() {
  local host="$1" out ip
  out="$("${TS_ARR[@]}" ip -4 "$host" 2>/dev/null)" || return 1
  ip="$(printf '%s\n' "$out" | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' | head -n1)"
  [ -n "$ip" ] || return 1
  printf '%s' "$ip"
}

# 規則のブロックを標準出力に出す。解決に失敗したら終了コード 1（何も出さない）
build_block() {
  local lines kind f1 f2 f3 ip out=""
  lines="$(read_profile)"
  # 行の形: E<TAB>メッセージ / T<TAB>id<TAB>host<TAB>port / A<TAB>host<TAB>port
  while IFS=$'\t' read -r kind f1 f2 f3; do
    [ -n "$kind" ] || continue
    case "$kind" in
      E) die "$f1" ;;
      T)
        ip="$(resolve_ip "$f2")" || die "環境 $f1 の宛先を tailscale ip -4 で解決できない"
        out+="-A DOCKER-USER -i ax-work -s ${RESOURCES_IP} -o tailscale0 -d ${ip} -p tcp --dport ${f3} -j ACCEPT"$'\n' ;;
      A)
        ip="$(resolve_ip "$f1")" || die "archive_dest の宛先を tailscale ip -4 で解決できない"
        out+="-A DOCKER-USER -i ax-core -s ${ARCHIVER_IP} -o tailscale0 -d ${ip} -p tcp --dport ${f2} -j ACCEPT"$'\n' ;;
    esac
  done <<<"$lines"
  printf '%s\n' "$BEGIN_MARK"
  printf '%s\n' "# 生成: host/ax-fw.sh（手で編集しない。次の実行で上書きされる。F-18.1）"
  printf '%s\n' "*filter"
  printf '%s\n' ":DOCKER-USER - [0:0]"
  printf '%s' "$out"
  printf '%s\n' "-A DOCKER-USER -i ax-core -o tailscale0 -j DROP"
  printf '%s\n' "-A DOCKER-USER -i ax-work -o tailscale0 -j DROP"
  printf '%s\n' "-A DOCKER-USER -i ax-job -o tailscale0 -j DROP"
  printf '%s\n' "-A DOCKER-USER -j RETURN"
  printf '%s\n' "COMMIT"
  printf '%s\n' "$END_MARK"
}

build_ufw_rules() {
  local self
  self="$(resolve_self_ip)" || die "ホスト自身の tailnet アドレスを tailscale ip -4 で解決できない"
  cat <<EOF
ufw allow in on ax-core to any port 3425 proto tcp
ufw allow in on ax-work to any port 3425 proto tcp
ufw allow in on ax-work from ${WORKER_POOL_IP} to any port 22 proto tcp
ufw allow in on ax-work from ${RESOURCES_IP} to any port 22 proto tcp
ufw allow in on ax-core to ${self} port 8377 proto tcp
ufw allow in on ax-core to ${self} port 8908 proto tcp
EOF
}

resolve_self_ip() {
  local out ip
  out="$("${TS_ARR[@]}" ip -4 2>/dev/null)" || return 1
  ip="$(printf '%s\n' "$out" | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' | head -n1)"
  [ -n "$ip" ] || return 1
  printf '%s' "$ip"
}

# 現在の after.rules の内容。読めなければ sudo で読む。無ければ空
read_after_rules() {
  if [ -r "$AFTER_RULES" ]; then cat "$AFTER_RULES"
  elif run_root test -e "$AFTER_RULES"; then run_root cat "$AFTER_RULES"
  else return 1; fi
}

# 現在の内容(stdin)の AX のブロックを新しいブロック($1)に替える。無ければ末尾に足す。印が壊れていれば終了コード 1
splice_block() {
  local block_file="$1"
  # stdin は after.rules の内容。コードは -c で渡す（heredoc だと stdin を奪う）
  python3 -P -c '
import sys
block = open(sys.argv[1], encoding="utf-8").read()
begin, end = sys.argv[2], sys.argv[3]
text = sys.stdin.read()
lines = text.split("\n")
bi = [i for i, l in enumerate(lines) if l.strip() == begin]
ei = [i for i, l in enumerate(lines) if l.strip() == end]
if len(bi) != len(ei) or len(bi) > 1 or (bi and ei[0] < bi[0]):
    sys.stderr.write("ax-fw: after.rules の # BEGIN AX / # END AX の印が壊れている（数か順序）。何も書かない\n")
    sys.exit(1)
if bi:
    new = lines[:bi[0]] + block.rstrip("\n").split("\n") + lines[ei[0] + 1:]
    out = "\n".join(new)
else:
    out = text
    if out and not out.endswith("\n"):
        out += "\n"
    out += ("\n" if out else "") + block
sys.stdout.write(out)
' "$block_file" "$BEGIN_MARK" "$END_MARK"
}

extract_block() {
  python3 -P -c '
import sys
begin, end = sys.argv[1], sys.argv[2]
lines = sys.stdin.read().split("\n")
bi = [i for i, l in enumerate(lines) if l.strip() == begin]
ei = [i for i, l in enumerate(lines) if l.strip() == end]
if len(bi) != 1 or len(ei) != 1 or ei[0] < bi[0]:
    sys.exit(1)
sys.stdout.write("\n".join(lines[bi[0]:ei[0] + 1]) + "\n")
' "$BEGIN_MARK" "$END_MARK"
}

TMPROOT="${TMPDIR:-/srv/hddtmp}"
mkdir -p "$TMPROOT"
WORK="$(mktemp -d "$TMPROOT/ax-fw.XXXXXX")"
trap 'rm -f "$WORK"/* 2>/dev/null; rmdir "$WORK" 2>/dev/null || true' EXIT

default_archive_dir() {
  printf '%s' "${HOME}/.local/share/ax/archive/ax-cutover-$(date -u +%Y%m%d)/fw"
}

next_archive_path() {  # $1=接頭辞 $2=接尾辞。同じ秒の控えを上書きしない。
  local stem="$1" suffix="$2" ts n=0 candidate
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  candidate="$ARCHIVE_DIR/${stem}.${ts}${suffix}"
  while [ -e "$candidate" ]; do
    n=$((n + 1))
    candidate="$ARCHIVE_DIR/${stem}.${ts}-${n}${suffix}"
  done
  printf '%s' "$candidate"
}

case "$CMD" in
  rules)
    build_block ;;
  ufw-rules)
    build_ufw_rules ;;
  check)
    build_block >"$WORK/block" || exit 1
    cur="$(read_after_rules)" || { echo "ax-fw: after.rules が無い" >&2; exit 1; }
    printf '%s\n' "$cur" | extract_block >"$WORK/cur_block" || { echo "ax-fw: AX のブロックが無い" >&2; exit 1; }
    if cmp -s "$WORK/block" "$WORK/cur_block"; then echo "ax-fw: 一致"; exit 0; fi
    echo "ax-fw: 違う（apply で反映する）" >&2; exit 1 ;;
  apply)
    build_block >"$WORK/block" || exit 1       # 解決に失敗したらここで止まる（何も書いていない）
    if read_after_rules >"$WORK/cur" 2>/dev/null; then :; else : >"$WORK/cur"; fi
    splice_block "$WORK/block" <"$WORK/cur" >"$WORK/new" || exit 1
    if cmp -s "$WORK/cur" "$WORK/new"; then echo "ax-fw: 変更なし"; exit 0; fi
    [ -n "$ARCHIVE_DIR" ] || ARCHIVE_DIR="$(default_archive_dir)"
    mkdir -p "$ARCHIVE_DIR" && chmod 700 "$ARCHIVE_DIR" || die "archive を作れない: $ARCHIVE_DIR"
    after_backup=""
    if [ -e "$AFTER_RULES" ] || run_root test -e "$AFTER_RULES"; then
      after_backup="$(next_archive_path after.rules '')"
      run_root cp -p "$AFTER_RULES" "$after_backup" || die "after.rules の控えを作れない"
    fi
    iptables_backup="$(next_archive_path DOCKER-USER .txt)"
    { run_root "${IPT_ARR[@]}" -S DOCKER-USER >"$iptables_backup"; } 2>/dev/null || echo "ax-fw: 注意: iptables -S DOCKER-USER を保存できなかった" >&2
    if [ -e "$AFTER_RULES" ]; then mode="$(stat -c %a "$AFTER_RULES" 2>/dev/null || echo 640)"; else mode=640; fi
    owner="${OWNER%%:*}"; group="${OWNER##*:}"
    run_root install -m "$mode" -o "$owner" -g "$group" "$WORK/new" "$AFTER_RULES.ax-new" || die "新しい内容を置けない"
    run_root mv -f "$AFTER_RULES.ax-new" "$AFTER_RULES" || die "after.rules を置き換えられない"
    if [ -n "$after_backup" ]; then
      echo "ax-fw: after.rules を更新した（控え: $after_backup）"
    else
      echo "ax-fw: after.rules を更新した（元の after.rules が無いため控えは無い）"
    fi
    if [ "$RELOAD" -eq 1 ]; then
      run_root "${UFW_ARR[@]}" reload || { echo "ax-fw: ufw reload に失敗した（after.rules は更新済み。控えから戻せる）" >&2; exit 3; }
      if run_root "${IPT_ARR[@]}" -S DOCKER-USER >"$WORK/after_ipt" 2>/dev/null && grep -q -- '-A DOCKER-USER -j RETURN' "$WORK/after_ipt"; then
        echo "ax-fw: DOCKER-USER に反映された"
      else
        echo "ax-fw: reload の後、DOCKER-USER に規則が見えない" >&2; exit 3
      fi
    fi
    exit 0 ;;
  ufw-apply)
    build_ufw_rules >"$WORK/ufw_rules" || exit 1
    [ -n "$ARCHIVE_DIR" ] || ARCHIVE_DIR="$(default_archive_dir)"
    mkdir -p "$ARCHIVE_DIR" && chmod 700 "$ARCHIVE_DIR" || die "archive を作れない: $ARCHIVE_DIR"
    ufw_backup="$(next_archive_path ufw-status-numbered .txt)"
    run_root "${UFW_ARR[@]}" status numbered >"$ufw_backup" 2>/dev/null || die "ufw status numbered を保存できない"
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      # 「ufw allow …」の ufw を $UFW に替えて、語に分けて実行する
      # shellcheck disable=SC2086
      run_root "${UFW_ARR[@]}" ${line#ufw } || die "ufw の規則を入れられない: $line"
    done <"$WORK/ufw_rules"
    echo "ax-fw: ufw の許可規則を入れた" ;;
esac

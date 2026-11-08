#!/usr/bin/env bash
# 部品（compose のサービス）を起動し、healthz を待ち、通ったときだけ見張りの登録を作る（F-18.6）。B0 の最小版を B8 が仕上げた。
# 仕上げ: 同じサービスの同時実行を ${ax_state_dir}/locks/up-<サービス名>.lock の flock -n で拒否、--dry-run（実行する内容の表示だけ。何も作らない）、
#         ~/.local/bin/ax へのリンク（$AX_HOME/host/ax があるとき。B12 から。F-9.2）、起動の結果の事象 deploy.up（hostwatch.log へ。host/lib/hostwatch/hostlog.py）、
#         host/ax-deploy.sh（F-18.3）からの呼び出し（変わった・新しいサービスを 1 つずつ）。
#
# 使い方: host/ax-up.sh [--profile FILE] [--dry-run] <サービス名>…
# 手順:
#   (1) 状態置き場を作る: ${ax_state_dir}/{deploy,commander,host-requests,hostwatch/registered,redact,jobs,locks,cutover}
#       commander/ と cutover/ は権限0700（既にあれば権限を確かめて直す）
#   (2) host/ax-env.sh で ${ax_state_dir}/deploy/profile.env を作る
#   (3) プラグインのサービスなら ${secrets_dir}/plugin-tokens/<id> を、無いときだけ作る（32バイトの乱数の16進、権限600）。
#       worker-pool と resources は ${secrets_dir}/core-tokens/worker-pool を、manager・intake・observer・gate は自身の名前の core-tokens を、無いときだけ同じ形で作り、
#       ${ax_state_dir}/core-tokens.sha256 に行 "<component> <sha256>" を足す。トークンの値は出力しない（F-18.4）
#   (4) ${ax_state_dir}/redact/index が無ければ host/ax-redact-index.sh を実行
#   (5) sudo -n docker compose -p ax --project-directory ${AX_HOME} --env-file …profile.env --env-file …current.env -f compose.yaml [-f compose.d/*.yaml …] up -d <サービス名>
#   (6) healthcheck が healthy になるまで docker inspect で最大 120 秒、2 秒間隔で待つ（healthcheck の無いサービスは running で足りる）。
#       ただし restart: "no" の一度きりのサービス（archive-init・b3-volume-init 等）は終了コード 0 で終わるのを待つ。登録しても見張りは触らない
#   (7) 通ったら ${ax_state_dir}/hostwatch/registered/<サービス名> を作る（空のファイル）
# 失敗は終了コード 1 で、登録しない。
# 環境変数: AX_PROFILE_FILE（--profile が無い場合の profile）、AX_HOME（clone の場所。既定 ~/.local/share/ax/prod）、AX_UP_SUDO（既定 "sudo -n"）、AX_UP_WAIT_S（既定 120）、AX_UP_INTERVAL_S（既定 2）、AX_BIN_DIR（ax のリンクの置き場。既定 ~/.local/bin）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PROFILE="${AX_PROFILE_FILE:-${HOME}/.config/ax/profile/default.yaml}"
SERVICES=()
DRY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) [ "$#" -ge 2 ] || { echo "ax-up: --profile に値が要る" >&2; exit 2; }; PROFILE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    -*) echo "ax-up: 知らない引数: $1" >&2; exit 2 ;;
    *) SERVICES+=("$1"); shift ;;
  esac
done
[ "${#SERVICES[@]}" -gt 0 ] || { echo "ax-up: サービス名が要る" >&2; exit 2; }
for s in "${SERVICES[@]}"; do
  case "$s" in *[!a-z0-9-]*|"") echo "ax-up: サービス名は英小文字・数字・ハイフン: $s" >&2; exit 2 ;; esac
done

# 監視を他の業務部品より先に起動し、先に登録する（D-15 の起動順、F-14.2）: observer を先頭に動かす。残りは指定の順
if [ "${#SERVICES[@]}" -gt 1 ]; then
  _first=(); _rest=()
  for s in "${SERVICES[@]}"; do if [ "$s" = observer ]; then _first+=("$s"); else _rest+=("$s"); fi; done
  SERVICES=("${_first[@]}" "${_rest[@]}")
fi

AX_HOME="${AX_HOME:-$HOME/.local/share/ax/prod}"
SUDO="${AX_UP_SUDO:-sudo -n}"
WAIT_S="${AX_UP_WAIT_S:-120}"
INTERVAL_S="${AX_UP_INTERVAL_S:-2}"
[ "$INTERVAL_S" -ge 1 ] 2>/dev/null || INTERVAL_S=1  # 0 や数字でない値では待ちが進まず終わらない
pyprofile() { PYTHONPATH="$REPO" AX_HOME="$AX_HOME" python3 -P -m axcommon.profile --profile "$PROFILE" --print "$1"; }
STATE="$(pyprofile AX_STATE_DIR)" || { echo "ax-up: profile を読めない（host/ax-env.sh --check で確かめる）" >&2; exit 1; }
SECRETS="$(pyprofile AX_SECRETS_DIR)" || exit 1

# 起動のコマンド（compose.d/ の各ファイルに -f を 1 つずつ付ける）
COMPOSE_FILES=(-f "$AX_HOME/compose.yaml")
shopt -s nullglob
for f in "$AX_HOME"/compose.d/*.yaml; do COMPOSE_FILES+=(-f "$f"); done
shopt -u nullglob
# Missing audit credentials must not prevent resources and unrelated jobs starting.
# Mount only the two dedicated files when both are present.
if [ -f "$SECRETS/env-ssh-noded" ] && [ -f "$SECRETS/env-ssh-noded-known-hosts" ]; then
  COMPOSE_FILES+=(-f "$AX_HOME/host/compose.noded-ssh.yaml")
fi
# 退避元の断片（F-20）: ${ax_state_dir}/deploy/archive-sources.compose.yaml。-f への追加は dry-run の表示と起動の直前（下）で行う
SOURCES_FILE="${AX_ARCHIVE_SOURCES_FILE:-$(dirname "$PROFILE")/archive-sources.yaml}"
FRAGMENT="$STATE/deploy/archive-sources.compose.yaml"
# shellcheck disable=SC2206
SUDO_CMD=($SUDO)
EXTRA_ENV_ARGS=()
INSTALL_ENV_ARGS=()
[ ! -f "$AX_HOME/config/launcher.env" ] || INSTALL_ENV_ARGS=(--env-file "$AX_HOME/config/launcher.env")
dc() {
  env -u AX_SCHEDULED_PUBLISHER_URL_PREFIXES python3 -P "$HERE/lib/hostwatch/ipam_compose.py" "${SUDO_CMD[@]}" docker compose -p ax --project-directory "$AX_HOME" \
    "${INSTALL_ENV_ARGS[@]}" --env-file "$STATE/deploy/profile.env" --env-file "$STATE/deploy/current.env" "${EXTRA_ENV_ARGS[@]}" "${COMPOSE_FILES[@]}" -- "$@"
}

# 全検査を、状態置き場と資格の作成より前に行う。
"$HERE/ax-env.sh" --profile "$PROFILE" --check >/dev/null || exit 1
PYTHONPATH="$REPO:$HERE/lib${PYTHONPATH:+:$PYTHONPATH}" AX_PROFILE="$PROFILE" AX_HOME="$AX_HOME" python3 -P - "${SERVICES[@]}" <<'PY'
import sys
from hostwatch.config import load
cfg = load()
if any(cfg.is_commander(name) for name in sys.argv[1:]):
    print("ax-up: 司令塔と Paseo は対象外", file=sys.stderr)
    sys.exit(1)
PY

# --dry-run: 実行する内容の表示だけ。ディレクトリ・資格・登録・コンテナのどれも作らない
if [ "$DRY" -eq 1 ]; then
  echo "ax-up(dry-run): 状態置き場 $STATE/{deploy,commander,host-requests,hostwatch/registered,redact,jobs,locks,cutover} を作る（commander と cutover は 0700）"
  echo "ax-up(dry-run): $HERE/ax-env.sh --profile $PROFILE --out $STATE/deploy/profile.env"
  echo "ax-up(dry-run): 資格（無いときだけ）: plugin-tokens/<id>、core-tokens/<component>（サービス名による）。値は表示しない"
  echo "ax-up(dry-run): 索引が無ければ $HERE/ax-redact-index.sh --profile $PROFILE"
  [ -f "$AX_HOME/host/ax" ] && echo "ax-up(dry-run): ${AX_BIN_DIR:-$HOME/.local/bin}/ax -> $AX_HOME/host/ax のリンク"
  # 起動のたびに再生成して -f に足す断片（F-20）。生成が失敗のときは実際の起動では断片を使わずに続ける
  [ -f "$SOURCES_FILE" ] && COMPOSE_FILES+=(-f "$FRAGMENT")
  for svc in "${SERVICES[@]}"; do
    echo "ax-up(dry-run): ${SUDO_CMD[*]} docker compose -p ax --project-directory $AX_HOME --env-file $STATE/deploy/profile.env --env-file $STATE/deploy/current.env ${COMPOSE_FILES[*]} up -d $svc"
    echo "ax-up(dry-run): $svc が healthy（healthcheck が無ければ running。restart: \"no\" なら正常終了）になるまで最大 ${WAIT_S} 秒、${INTERVAL_S} 秒間隔で待ち、通ったら $STATE/hostwatch/registered/$svc を作る"
  done
  exit 0
fi

# 事象 deploy.* を hostwatch.log へ足す（ホストは時系列記録に届かない。オブザーバーが戻ったら写す）。書けなくても止めない
HOSTNAME_AX="$(pyprofile AX_HOST 2>/dev/null || echo unknown)"
emit_event() {  # $1=kind $2=subject 残り=key=value
  PYTHONPATH="$REPO:$HERE/lib${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m hostwatch.hostlog "$STATE" "$HOSTNAME_AX" "$@" >/dev/null 2>&1 || true
}

# Explicit inputs must exist; unset inputs keep the Compose /dev/null defaults.
if printf '%s\n' "${SERVICES[@]}" | grep -qx 'timer-runner'; then
  if [ -n "${AX_CONVERSATION_HISTORY_HOST_DIR:-}" ] && [ ! -d "$AX_CONVERSATION_HISTORY_HOST_DIR" ]; then
    echo "ax-up: AX_CONVERSATION_HISTORY_HOST_DIR must be an existing directory" >&2
    exit 1
  fi
  if [ -n "${AX_EMBEDDING_TOKEN_HOST_FILE:-}" ] && [ ! -f "$AX_EMBEDDING_TOKEN_HOST_FILE" ]; then
    echo "ax-up: AX_EMBEDDING_TOKEN_HOST_FILE must be an existing file" >&2
    exit 1
  fi
fi

# (1) 状態置き場
umask 077
for d in deploy commander host-requests hostwatch/registered redact jobs locks cutover; do
  mkdir -p "$STATE/$d"
done
chmod 0700 "$STATE/commander" "$STATE/cutover"
# redact/ は索引（0644）を他のコンテナが読むので 0755 にする。他は operator だけで足りるが、既存の権限を勝手に狭めない
chmod 0755 "$STATE/redact"

# 退避元の断片を起動のたびに再生成する（F-20）。archive-sources.yaml が無い・生成が失敗のときは、
# 断片を使わずに起動を続ける（fail-soft）。警告には archiver が退避元を mount できないことを明記する
archive_dir() {
  local root="${TMPDIR:-/srv/hddtmp}/archive/ax-up"
  mkdir -p -m 700 "$root" && mktemp -d "$root/run.XXXXXXXX"
}
if [ -f "$SOURCES_FILE" ]; then
  if genarchive="$(archive_dir)"; then
    genlog="$genarchive/gen_sources.log"
    if PYTHONPATH="$REPO" python3 -P -m core.archiver.gen_sources_compose \
         --sources "$SOURCES_FILE" --out "$FRAGMENT.tmp.$$" >"$genlog" 2>&1; then
      grep -q . "$genlog" && sed 's/^/ax-up: gen_sources_compose: /' "$genlog" >&2
      if { [ ! -e "$FRAGMENT" ] || cp -p -- "$FRAGMENT" "$genarchive/previous-fragment"; } &&
         mv "$FRAGMENT.tmp.$$" "$FRAGMENT"; then
        COMPOSE_FILES+=(-f "$FRAGMENT")
        echo "ax-up: 退避元の断片を再生成した: $FRAGMENT"
      else
        echo "ax-up: 警告: 断片を保存・公開できないため、この起動では使用しない" >&2
      fi
    else
      sed 's/^/ax-up: gen_sources_compose: /' "$genlog" >&2 || true
      echo "ax-up: 警告: 退避元の断片を作れないため、この起動では archiver が退避元（$SOURCES_FILE の一覧）を mount できない。ボリュームの archive/ だけが退避対象になる" >&2
    fi
    if [ -e "$FRAGMENT.tmp.$$" ]; then
      mv -- "$FRAGMENT.tmp.$$" "$genarchive/generated-fragment" || echo "ax-up: 警告: 生成途中の断片を退避できない" >&2
    fi
  else
    echo "ax-up: 警告: 生成ログの退避先を作れないため、断片を生成しない" >&2
  fi
elif [ -f "$FRAGMENT" ]; then
  # 一覧が無くなったのに断片が残っている: 退避元の bind は渡さない（無い物は登録しない。F-10）。残った断片は使わない
  echo "ax-up: 警告: 退避元の一覧が無いので、この起動では archiver が退避元を mount できない。残った断片 $FRAGMENT は使わない" >&2
fi

# 同じサービスの同時実行を拒否する（flock -n）。ロックは終了時に自動で放される
for svc in "${SERVICES[@]}"; do
  exec {lfd}>"$STATE/locks/up-$svc.lock"
  flock -n "$lfd" || { echo "ax-up: $svc は別の ax-up が実行中" >&2; exit 1; }
done

# (2) profile.env（B8 の検査つき）
"$HERE/ax-env.sh" --profile "$PROFILE" --out "$STATE/deploy/profile.env" >/dev/null || { echo "ax-up: ax-env.sh が失敗" >&2; exit 1; }

# Compose loads current.env after profile.env; use the same effective intake UID.
intake_uid="$(sed -n 's/^AX_UID=//p' "$STATE/deploy/profile.env" | tail -n1)"
if [ -f "$STATE/deploy/current.env" ]; then
  current_intake_uid="$(sed -n 's/^AX_UID=//p' "$STATE/deploy/current.env" | tail -n1)"
  [ -z "$current_intake_uid" ] || intake_uid="$current_intake_uid"
fi

# Prepare the SSH seat socket directory without replacing existing contents.
if printf '%s\n' "${SERVICES[@]}" | grep -qx 'intake'; then
  ax_uid="$intake_uid"
  seat_uid="$(id -u ax-seat 2>/dev/null || true)"
  seat_gid="$(id -g ax-seat 2>/dev/null || true)"
  if [[ "$ax_uid" =~ ^[0-9]+$ && "$seat_uid" =~ ^[0-9]+$ && "$seat_gid" =~ ^[0-9]+$ ]]; then
    if [ ! -L "$STATE/seat" ] && mkdir -p "$STATE/seat" &&
       "${SUDO_CMD[@]}" chown "$ax_uid:$seat_gid" "$STATE/seat" &&
       "${SUDO_CMD[@]}" chmod 2710 "$STATE/seat"; then
      printf 'AX_SEAT_UID=%s\nAX_SEAT_GID=%s\n' "$seat_uid" "$seat_gid" >> "$STATE/deploy/profile.env"
    else
      echo "ax-up: seat socket preparation unavailable" >&2
    fi
  else
    # Compose needs a bind source even when the dedicated account is absent.
    [ ! -L "$STATE/seat" ] && mkdir -p -m 0700 "$STATE/seat" || echo "ax-up: seat socket preparation unavailable" >&2
    echo "ax-up: seat identity unavailable" >&2
  fi
fi

# Prepare the HQ proof key and socket directory only for the HQ/intake services.
# Missing HQ identity leaves privileged actions unavailable without stopping other services.
if printf '%s\n' "${SERVICES[@]}" | grep -qxE '(hq|intake)'; then
  hq_gid="$(sed -n 's/^AX_HQ_GID=//p' "$STATE/deploy/profile.env")"
  ax_uid="$intake_uid"
  if [[ "$hq_gid" =~ ^[0-9]+$ && "$ax_uid" =~ ^[0-9]+$ ]]; then
    (
      flock -x 9
      [ ! -L "$SECRETS/hq-proof" ] && [ ! -L "$STATE/hq-intake" ] || exit 1
      mkdir -p "$SECRETS/hq-proof" "$STATE/hq-intake" || exit 1
      [ ! -L "$SECRETS/hq-proof/key" ] || exit 1
      if [ ! -e "$SECRETS/hq-proof/key" ]; then
        HQ_PROOF_FILE="$SECRETS/hq-proof/key" python3 - <<'HQPROOF' || exit 1
import os
import secrets
fd = os.open(os.environ['HQ_PROOF_FILE'], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'wb') as stream:
    stream.write(secrets.token_bytes(32))
HQPROOF
      fi
      [ -f "$SECRETS/hq-proof/key" ] && [ "$(stat -c %h "$SECRETS/hq-proof/key")" = 1 ] || exit 1
      "${SUDO_CMD[@]}" chown "$ax_uid:$hq_gid" "$SECRETS/hq-proof" "$SECRETS/hq-proof/key" "$STATE/hq-intake" || exit 1
      chmod 0710 "$SECRETS/hq-proof" || exit 1
      chmod 0640 "$SECRETS/hq-proof/key" || exit 1
      "${SUDO_CMD[@]}" chmod 2710 "$STATE/hq-intake" || exit 1
    ) 9>"$STATE/locks/hq-proof.lock" || echo "ax-up: HQ proof preparation unavailable" >&2
  else
    echo "ax-up: HQ proof preparation unavailable" >&2
  fi
fi

# Optional, explicitly configured read-only inputs for conversation indexing.
# An absent or invalid source leaves this timer unavailable without stopping others.
if printf '%s\n' "${SERVICES[@]}" | grep -qx 'timer-runner' &&
   [ "${AX_CONVERSATION_INDEX_ENABLED:-}" = "true" ] &&
   [ -n "${AX_CONVERSATION_HISTORY_HOST_DIR:-}" ] && [ -n "${AX_EMBEDDING_TOKEN_HOST_FILE:-}" ]; then
  index_fragment="$STATE/deploy/conversation-index.compose.yaml"
  if AX_INDEX_FRAGMENT="$index_fragment" python3 - <<'INDEXMOUNT'
import os
from pathlib import Path
import yaml
history = Path(os.environ['AX_CONVERSATION_HISTORY_HOST_DIR'])
token = Path(os.environ['AX_EMBEDDING_TOKEN_HOST_FILE'])
if not history.is_absolute() or not token.is_absolute() or history.is_symlink() or token.is_symlink():
    raise SystemExit(1)
if not history.is_dir() or not token.is_file():
    raise SystemExit(1)
fragment = {'services': {'timer-runner': {
    'environment': {'AX_CONVERSATION_HISTORY_DIR': '/conversation-history',
                    'AX_EMBEDDING_TOKEN_FILE': '/run/embedding/token'},
    'volumes': [{'type': 'bind', 'source': str(history.resolve()), 'target': '/conversation-history', 'read_only': True},
                {'type': 'bind', 'source': str(token.resolve()), 'target': '/run/embedding/token', 'read_only': True}]}}}
fd = os.open(os.environ['AX_INDEX_FRAGMENT'], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as stream:
    yaml.safe_dump(fragment, stream, sort_keys=False)
INDEXMOUNT
  then
    COMPOSE_FILES+=(-f "$index_fragment")
  else
    echo "ax-up: conversation index input unavailable" >&2
  fi
fi

# Each optional input is isolated; invalid sources never bind a default file.
if launcher_outputs="$(python3 "$HERE/lib/launcher_inputs.py" "$AX_HOME" "$STATE" "$$" "${SERVICES[@]}")"; then
  while IFS= read -r output; do
    case "$output" in
      fragment\ *) COMPOSE_FILES+=(-f "${output#fragment }") ;;
      env\ *) EXTRA_ENV_ARGS+=(--env-file "${output#env }") ;;
    esac
  done <<< "$launcher_outputs"
else
  echo "ax-up: optional launcher inputs unavailable" >&2
fi

# AX_TAG: ax-deploy.sh（B8 以降）が無い間は、最初の1回だけ main の短い ID（12桁）を current.env に書く
if [ ! -f "$STATE/deploy/current.env" ]; then
  tag="$(git -C "$AX_HOME" rev-parse --short=12 main 2>/dev/null || true)"
  [ -n "$tag" ] || { echo "ax-up: AX_TAG を決められない（$AX_HOME に main が無い）" >&2; exit 1; }
  printf 'AX_TAG=%s\n' "$tag" > "$STATE/deploy/current.env.tmp.$$"
  chmod 0600 "$STATE/deploy/current.env.tmp.$$"
  if [ -e "$STATE/deploy/current.env" ]; then
    current_archive="$(archive_dir)" || { echo "ax-up: current.env の退避先を作れない" >&2; exit 1; }
    cp -p -- "$STATE/deploy/current.env" "$current_archive/previous-current.env" || { echo "ax-up: current.env を退避できない" >&2; exit 1; }
  fi
  mv "$STATE/deploy/current.env.tmp.$$" "$STATE/deploy/current.env"
fi

# observer の bind 元は通常ファイルが必要。既存ファイルの inode と内容は保つ。
if printf '%s\n' "${SERVICES[@]}" | grep -qx 'observer'; then
  observer_uid="$(sed -n 's/^AX_UID=//p' "$STATE/deploy/profile.env" | tail -n1)"
  observer_gid="$(sed -n 's/^AX_GID=//p' "$STATE/deploy/profile.env" | tail -n1)"
  current_uid="$(sed -n 's/^AX_UID=//p' "$STATE/deploy/current.env" | tail -n1)"
  current_gid="$(sed -n 's/^AX_GID=//p' "$STATE/deploy/current.env" | tail -n1)"
  [ -z "$current_uid" ] || observer_uid="$current_uid"
  [ -z "$current_gid" ] || observer_gid="$current_gid"
  [[ "$observer_uid" =~ ^[0-9]+$ && "$observer_gid" =~ ^[0-9]+$ ]] || { echo "ax-up: observer の UID/GID が不正" >&2; exit 1; }
  observer_beat="$STATE/observer.beat"
  if [ -L "$observer_beat" ] || { [ -e "$observer_beat" ] && [ ! -f "$observer_beat" ]; }; then
    echo "ax-up: observer.beat は通常ファイルでなければならない" >&2; exit 1
  fi
  if [ ! -e "$observer_beat" ]; then
    ( set -o noclobber; umask 077; : > "$observer_beat" ) || { echo "ax-up: observer.beat を作れない" >&2; exit 1; }
    chmod 0644 "$observer_beat" || { echo "ax-up: observer.beat の権限を設定できない" >&2; exit 1; }
    "${SUDO_CMD[@]}" chown "$observer_uid:$observer_gid" "$observer_beat" || { echo "ax-up: observer.beat の所有者を設定できない" >&2; exit 1; }
  fi
fi

# (3) 資格
new_token() { python3 -c 'import secrets; print(secrets.token_hex(32), end="")'; }
ensure_token_file() {  # $1=ファイル。無いときだけ作る。作ったら 0 を返す
  local f="$1"
  [ -e "$f" ] && return 1
  mkdir -p "$(dirname "$f")"
  chmod 0700 "$(dirname "$f")" 2>/dev/null || true
  ( umask 077; new_token > "$f.tmp.$$"; chmod 0600 "$f.tmp.$$"; mv "$f.tmp.$$" "$f" )
  return 0
}
plugin_ids_for() {  # $1=サービス名 -> 標準出力にプラグイン ID（0 個以上）
  AX_REG="$AX_HOME/plugins/registry.yaml" AX_SVC="$1" python3 - <<'PY'
import os, sys
try:
    import yaml
    reg = yaml.safe_load(open(os.environ["AX_REG"], encoding="utf-8")) or {}
except Exception:
    sys.exit(0)  # 登録簿が無い、または読めない: プラグインではないとみなす
plugins = reg.get("plugins") or []
svc = os.environ["AX_SVC"]
if svc == "timer-runner":
    for p in plugins:
        if p.get("kind") == "timer":
            pid = p["id"]
            print(pid)
            try:
                from pathlib import Path
                root = Path(os.environ["AX_REG"]).resolve().parent.parent
                path = (root / p["path"]).resolve()
                if not path.is_relative_to(root):
                    continue
                manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict) or manifest.get("id") != pid:
                    continue
                caps = manifest.get("memory_capabilities")
                if caps == ["memory.read"]:
                    print(f"memory-read--{pid} memory.read:{pid}")
                elif caps == ["memory.write"]:
                    print(f"memory-write--{pid} memory.write:{pid}")
            except (OSError, KeyError, TypeError, yaml.YAMLError):
                continue
else:
    sid = svc[3:] if svc.startswith("sl-") else svc
    for p in plugins:
        if p.get("id") == sid and p.get("kind") in ("subloop", "resource"):
            print(sid)
PY
}
# Prepare both bind-mounted tables even if no credentials are registered yet.
python3 - "$STATE/core-tokens.sha256" <<'CORETABLE'
import os, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
os.close(fd)
CORETABLE
plugin_token_table="${AX_PLUGIN_TOKENS_FILE:-$STATE/plugin-tokens.sha256}"
(
  flock -x 9
  AX_PLUGIN_TABLE="$plugin_token_table" python3 - <<'PLUGINTABLE'
import os
fd = os.open(os.environ['AX_PLUGIN_TABLE'], os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
os.close(fd)
PLUGINTABLE
) 9>"$STATE/locks/plugin-tokens.lock" || echo "ax-up: plugin credential table unavailable" >&2
for svc in "${SERVICES[@]}"; do
  while read -r pid table_id; do
    [ -n "$pid" ] || continue
    table_id="${table_id:-$pid}"
    if [[ ! "$pid" =~ ^[a-z0-9][a-z0-9-]{0,127}$ ]]; then
      echo "ax-up: invalid plugin identifier" >&2
      continue
    fi
    if ensure_token_file "$SECRETS/plugin-tokens/$pid"; then echo "ax-up: plugin-tokens/$pid を作った（値は表示しない）"; fi
    if ! (
      flock -x 9
      AX_PLUGIN_TABLE="$plugin_token_table" AX_PLUGIN_ID="$table_id" AX_PLUGIN_FILE="$SECRETS/plugin-tokens/$pid" python3 - <<'PLUGINHASH'
import hashlib, os, stat
from pathlib import Path
name = os.environ['AX_PLUGIN_ID']
value = Path(os.environ['AX_PLUGIN_FILE']).read_text(encoding='utf-8').strip()
if not value:
    raise SystemExit(1)
digest = hashlib.sha256(value.encode()).hexdigest()
fd = os.open(os.environ['AX_PLUGIN_TABLE'], os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
with os.fdopen(fd, 'a+', encoding='utf-8') as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SystemExit(1)
    stream.seek(0)
    text = stream.read()
    rows = [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    existing = [row for row in rows if row and row[0] == name]
    if existing:
        if len(existing) != 1 or existing[0] != [name, digest]:
            raise SystemExit(1)
    else:
        stream.write(('\n' if text and not text.endswith('\n') else '') + name + ' ' + digest + '\n')
PLUGINHASH
    ) 9>"$STATE/locks/plugin-tokens.lock"; then
      echo "ax-up: plugin credential registration unavailable for $pid" >&2
    fi
  done < <(plugin_ids_for "$svc")
  core_component=""
  case "$svc" in
    worker-pool|resources) core_component="worker-pool" ;;
    emergency) core_component="observer" ;;
    sl-release) core_component="release-review" ;;
    manager|intake|observer|gate|control|hq) core_component="$svc" ;;
  esac
  if [ -n "$core_component" ]; then
    ensure_token_file "$SECRETS/core-tokens/$core_component" && echo "ax-up: core-tokens/$core_component を作った（値は表示しない）" || true
    sha="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read().decode().strip().encode()).hexdigest())' "$SECRETS/core-tokens/$core_component")"
    if [ "$core_component" = hq ]; then
      hq_gid="$(sed -n 's/^AX_HQ_GID=//p' "$STATE/deploy/profile.env")"
      if [[ "$hq_gid" =~ ^[0-9]+$ ]] && [ ! -L "$SECRETS/core-tokens/hq" ] && [ "$(stat -c %h "$SECRETS/core-tokens/hq")" = 1 ]; then
        "${SUDO_CMD[@]}" chgrp "$hq_gid" "$SECRETS/core-tokens/hq" && chmod 0640 "$SECRETS/core-tokens/hq" || echo "ax-up: HQ pool credential permissions unavailable" >&2
      fi
    fi
    lst="$STATE/core-tokens.sha256"
    if ! grep -q "^$core_component " "$lst" 2>/dev/null; then
      printf '%s %s\n' "$core_component" "$sha" >> "$lst"
      chmod 0644 "$lst"
      echo "ax-up: core-tokens.sha256 に $core_component の行を足した"
    fi
  fi
done

# (4) 索引
[ -f "$STATE/redact/index" ] || "$HERE/ax-redact-index.sh" --profile "$PROFILE" >/dev/null || { echo "ax-up: 索引を作れない" >&2; exit 1; }

# Add timer credentials when the overlay can be generated; other services continue on failure.
if printf '%s\n' "${SERVICES[@]}" | grep -qx 'timer-runner'; then
  timer_token_fragment="$STATE/deploy/timer-tokens.compose.json"
  if PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m runtime.timer_runner.token_compose \
      "$AX_HOME/plugins/registry.yaml" "$SECRETS" > "$timer_token_fragment"; then
    COMPOSE_FILES+=(-f "$timer_token_fragment")
  else
    echo "ax-up: timer token overlay unavailable" >&2
  fi
fi

# (5) 起動
# ax のリンク（B12 から。host/ax が clone に入ってから）
AX_BIN="${AX_BIN_DIR:-$HOME/.local/bin}"
if [ -f "$AX_HOME/host/ax" ]; then
  mkdir -p "$AX_BIN"
  if [ -L "$AX_BIN/ax" ] || [ ! -e "$AX_BIN/ax" ]; then ln -sfn "$AX_HOME/host/ax" "$AX_BIN/ax"
  else echo "ax-up: 注意: $AX_BIN/ax が既にある（リンクではない）。上書きしない" >&2; fi
fi

# (5) 起動と (6) healthy の確認をサービスごとに行い、(7) の登録を済ませてから次へ進む。
for svc in "${SERVICES[@]}"; do
  up_args=(up -d)
  [ "${AX_UP_NO_DEPS:-0}" = 1 ] && up_args+=(--no-deps)
  if ! dc "${up_args[@]}" "$svc"; then
    emit_event deploy.up "$svc" "service=$svc" "result=compose_up_failed"
    echo "ax-up: compose up が失敗" >&2; exit 1
  fi
  cid="$(dc ps -q "$svc" 2>/dev/null | head -n1 || true)"
  [ -n "$cid" ] || { emit_event deploy.up "$svc" "service=$svc" "result=no_container"; echo "ax-up: $svc のコンテナが見つからない" >&2; exit 1; }
  # 一度きりのサービス（restart: "no"）は healthy ではなく正常終了（exit 0）を待つ（deploy.py の changed_services と同じ判定）
  oneshot=0
  if [ "$(dc config --format json "$svc" 2>/dev/null | python3 -c 'import json,sys; print(str((json.load(sys.stdin).get("restart") or "").lower() == "no").lower())' 2>/dev/null || echo false)" = true ]; then
    oneshot=1
    # compose up -d は init コンテナを裏で走らせるので、終了を待つ
    "${SUDO_CMD[@]}" docker wait "$cid" >/dev/null 2>&1 || true
  fi
  waited=0
  ok=0
  while [ "$waited" -le "$WAIT_S" ]; do
    if [ "$oneshot" = 1 ]; then
      st="$("${SUDO_CMD[@]}" docker inspect --format '{{if .State.Running}}running{{else}}{{.State.Status}}{{end}} exit={{.State.ExitCode}}' "$cid" 2>/dev/null || echo error)"
      case "$st" in
        exited\ exit=0) ok=1; break ;;
        dead) break ;;
      esac
    else
      st="$("${SUDO_CMD[@]}" docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{if .State.Running}}running{{else}}stopped{{end}}{{end}}' "$cid" 2>/dev/null || echo error)"
      case "$st" in healthy|running) ok=1; break ;; esac
    fi
    [ "$waited" -lt "$WAIT_S" ] || break
    delay="$INTERVAL_S"
    [ "$delay" -le "$((WAIT_S - waited))" ] || delay="$((WAIT_S - waited))"
    sleep "$delay"
    waited=$((waited + delay))
  done
  if [ "$ok" -ne 1 ]; then
    if [ "$oneshot" = 1 ]; then
      emit_event deploy.up "$svc" "service=$svc" "result=not_completed" "state=$st"
      echo "ax-up: $svc（一度きりのサービス）が $WAIT_S 秒以内に正常終了しない（最後の状態: $st）。依存するサービスは起動しない" >&2
    else
      emit_event deploy.up "$svc" "service=$svc" "result=not_healthy" "state=$st"
      echo "ax-up: $svc が $WAIT_S 秒以内に healthy にならない（最後の状態: $st）。見張りに登録しない" >&2
    fi
    exit 1
  fi
  : > "$STATE/hostwatch/registered/$svc"
  emit_event deploy.up "$svc" "service=$svc" "result=ok" "state=$st"
  if [ "$oneshot" = 1 ]; then
    echo "ax-up: $svc は $st。一度きりのサービスなので見張りには登録しない"
  else
    echo "ax-up: $svc は $st。見張りに登録した"
  fi
done

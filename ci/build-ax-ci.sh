#!/usr/bin/env bash
# ax-ci イメージを組み立てる（ホストで行う。F-14.1）。B0 では実行していない（docker build とネットワークを使うため）。
# 使い方: ci/build-ax-ci.sh <タグ>      例: ci/build-ax-ci.sh 1
# 実行は ビルド用ホストの管理者 が行う: `sudo -n` を付けて docker を呼ぶ。ロックの digest と uv の版が UNVERIFIED のままなら、何もせず終了コード 1。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TAG="${1:?タグを渡す（例 1）}"

lockval() { sed -n "s/^$2=//p" "$HERE/$1" | head -n1; }
py_digest="$(lockval base-image.lock digest)"
cli_digest="$(lockval docker-cli.lock digest)"
git_digest="$(lockval git-image.lock digest)"
uv_ver="$(head -n1 "$HERE/uv.version")"
gitleaks_ver="$(lockval gitleaks.lock version)"
gitleaks_url="$(lockval gitleaks.lock url)"
for pair in "base-image.lock:$py_digest" "docker-cli.lock:$cli_digest" "git-image.lock:$git_digest" "uv.version:$uv_ver" "gitleaks.lock version:$gitleaks_ver" "gitleaks.lock url:$gitleaks_url"; do
  case "${pair#*:}" in ""|UNVERIFIED) echo "build-ax-ci: ${pair%%:*} が未確認（UNVERIFIED）。先に実環境で確かめて書く" >&2; exit 1 ;; esac
done
[ -s "$ROOT/uv.lock" ] && ! grep -q '^# 未生成' "$ROOT/uv.lock" || { echo "build-ax-ci: uv.lock が未生成。ビルド環境で uv lock を実行する" >&2; exit 1; }

exec sudo -n docker build -f "$HERE/Dockerfile.ax-ci" \
  --build-arg "DOCKERCLI_REF=docker:29.1.3-cli@sha256:${cli_digest#sha256:}" \
  --build-arg "GIT_REF=python:3.12-trixie@sha256:${git_digest#sha256:}" \
  --build-arg "PYTHON_REF=python:3.12-slim@sha256:${py_digest#sha256:}" \
  --build-arg "UV_VERSION=${uv_ver}" \
  --build-arg "GITLEAKS_URL=${gitleaks_url}" \
  -t "ax-ci:${TAG}" "$ROOT"

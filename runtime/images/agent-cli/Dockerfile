# agent-cli: Claude Code を入れた作業用イメージ（F-19.1）。ARG BASE で言語別のイメージに重ねる。
#   ax/agent-cli          BASE=debian:trixie-slim（既定）
#   ax/agent-cli-node     BASE=ax/verify-node:<AX_TAG>
#   ax/agent-cli-python   BASE=ax/verify-python:<AX_TAG>
#   ax/agent-cli-dotnet   BASE=ax/verify-dotnet:<AX_TAG>
# claude の実体は vendor/claude（host/ax-deploy.sh がビルドの前に ~/.local/share/mise/installs/claude/<CLAUDE_VERSION>/claude を cp -p する。
# vendor/ は git に置かない）。ファイルが無ければビルドしない。ベースの固定は base-images.lock。
# USER は docker run --user で上書きされる（root では動かさない。--dangerously-skip-permissions は root では使えない）。
# このファイルのビルドは B6b では未実行（docker build を使わない）。
ARG BASE=debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
FROM ${BASE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates openssh-client ripgrep curl jq python3 \
    && apt-get clean && find /var/lib/apt/lists -type f -delete
COPY vendor/claude /usr/local/bin/claude
ENV DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /work

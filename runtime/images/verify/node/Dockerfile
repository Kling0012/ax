# verify-node: example-org/example-node 用（pnpm、python3、uv）。F-19.2。旧 gh-auto-test-node の内容を写した。
# UV_IMAGE と NODE_IMAGE は base-images.lock のダイジェストで固定する。
ARG UV_IMAGE=ghcr.io/astral-sh/uv:latest@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490
ARG NODE_IMAGE=node:22-trixie-slim@sha256:be1e2a3dc2c4fd68cdfadad2a696a280e0d2749098bee509230174d72fb768c6
FROM ${UV_IMAGE} AS uvbin
FROM ${NODE_IMAGE}
COPY --from=uvbin /uv /uvx /usr/local/bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && apt-get clean && find /var/lib/apt/lists -type f -delete \
    && npm install -g pnpm@10.34.6

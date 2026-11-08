# verify-python: example-org/example-python 用（python 3.13、uv）。F-19.2。旧 gh-auto-test-python の内容を写した。
ARG UV_IMAGE=ghcr.io/astral-sh/uv:latest@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490
ARG PYTHON_IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
FROM ${UV_IMAGE} AS uvbin
FROM ${PYTHON_IMAGE}
COPY --from=uvbin /uv /uvx /usr/local/bin/
COPY read_verify.py /opt/ax/read_verify.py
RUN chmod -R a+rX /opt/ax

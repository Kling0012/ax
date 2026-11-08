# ax/archiver（F-20）: python:3.12-slim に openssh-client と rsync を足す。archive-init も同じイメージ。
# 組み立てはホストの host/ax-deploy.sh（B8）か compose の build。B1 では実行していない（docker build を使わない）。
ARG PYTHON_REF=python:3.12-slim
FROM ${PYTHON_REF}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/ax \
    AX_SCHEMAS_DIR=/ax/schemas \
    AX_CONFIG_DIR=/ax/config
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client rsync \
    && apt-get clean
ARG UV_VERSION
RUN test -n "${UV_VERSION}" && pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock /deps/
WORKDIR /deps
RUN uv sync --locked --no-install-project --no-dev
WORKDIR /ax
COPY . /ax
CMD ["python", "-m", "core.archiver"]

# chat-json: 判定だけの HTTP 呼び出しの runner（F-19.1、F-7.2）。python:3.12-slim に axcommon を入れた物。agent-cli とは別。
# ビルドの context は AX のリポジトリの根（compose.d/b6b.yaml の img-chat-json）。入れるのは axcommon/ だけ
# （Dockerfile.dockerignore で絞る）。jsonschema の版は uv.lock の値を build-arg で渡す（host/ax-deploy.sh。無ければビルドしない）。
# ジョブ用コンテナ（ax-job）で動くので、モデルの入口は ANTHROPIC_BASE_URL（launcher が固定）だけを読む。
ARG PYTHON_REF=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
FROM ${PYTHON_REF}
ARG JSONSCHEMA_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/ax
RUN test -n "${JSONSCHEMA_VERSION}" && pip install --no-cache-dir "jsonschema==${JSONSCHEMA_VERSION}"
COPY axcommon /ax/axcommon
RUN chmod -R a+rX /ax/axcommon
WORKDIR /work
CMD ["python", "-m", "axcommon.chat_json"]

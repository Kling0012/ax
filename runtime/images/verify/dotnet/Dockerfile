# verify-dotnet: Dreamforge の repo 用（.NET SDK 10、python3、git）。F-19.2。旧 gh-auto-test-dotnet の内容を写した。
# python コマンドの別名は /usr/local/bin/python（試験の道具が python を呼ぶため）。
ARG DOTNET_IMAGE=mcr.microsoft.com/dotnet/sdk:10.0@sha256:212fe4cb74627d5adf9817a4088906ae3edff1bd853bb27565d4b46731efe61e
FROM ${DOTNET_IMAGE}
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 \
    && apt-get clean && find /var/lib/apt/lists -type f -delete
RUN ln -sf "$(command -v python3)" /usr/local/bin/python

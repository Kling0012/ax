#!/usr/bin/env python3
"""偽の sudo（B8 のホストのスクリプトの試験用）。-n などのオプションを読み捨て、docker は実行せず、決まった出力を返す。実際の sudo・docker は呼ばない。

環境変数:
  FAKE_SUDO_LOG      あれば、呼び出し（残りの引数）を 1 行の JSON で追記する
  FAKE_SUDO_MODE     ok（既定）/ deny（`true` を含め、すべて終了コード 1）
  FAKE_PS_OUT        `docker compose … ps` の出力に使うファイル（既定: 空）
  FAKE_SERVICES      `docker compose … config --services` の出力に使うファイル
  FAKE_INSPECT       `docker inspect` の出力（既定 healthy）
  FAKE_UP_RC         `docker compose … up` の終了コード（既定 0）
"""
import json
import os
import sys

argv = sys.argv[1:]
while argv and argv[0].startswith("-"):
    argv.pop(0)
log = os.environ.get("FAKE_SUDO_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\n")
if os.environ.get("FAKE_SUDO_MODE") == "deny":
    print("sudo: a password is required", file=sys.stderr)
    sys.exit(1)
if argv == ["true"]:
    sys.exit(0)


def emit(var):
    p = os.environ.get(var)
    if p and os.path.exists(p):
        sys.stdout.write(open(p).read())


if argv[:3] == ["docker", "network", "ls"]:
    if os.environ.get("FAKE_NETWORK_FAILURE"):
        sys.exit(1)
    path = os.environ.get("FAKE_NETWORKS_JSON")
    values = json.load(open(path)) if path else []
    print("\n".join(x["Name"] for x in values))
    sys.exit(0)
if argv[:3] == ["docker", "network", "inspect"]:
    path = os.environ.get("FAKE_NETWORKS_JSON")
    values = json.load(open(path)) if path else []
    matches = [x for x in values if x["Name"] == argv[-1]]
    print(json.dumps(matches))
    sys.exit(0 if matches else 1)

if argv[:2] == ["docker", "compose"] and "--format" in argv and "json" in argv and "config" in argv and os.environ.get("FAKE_IPAM_REAL") == "1":
    import subprocess
    result = subprocess.run(argv, env={**os.environ, "DOCKER_HOST": "unix:///nonexistent"})
    sys.exit(result.returncode)

if argv[:2] == ["docker", "compose"] and "--format" in argv and "json" in argv and "config" in argv:
    import yaml
    networks = {}
    for name, subnet, gateway in (("ax-core", "192.0.2.0/25", "192.0.2.1"),
                                  ("ax-work", "198.51.100.0/25", "198.51.100.1"),
                                  ("ax-publish-review", "192.0.2.128/26", "192.0.2.129"),
                                  ("ax-job", "203.0.113.0/25", "203.0.113.1")):
        networks[name] = {"name": "ax_" + name, "ipam": {"config": [{"subnet": subnet, "gateway": gateway}]}}
    for i, arg in enumerate(argv[:-1]):
        if arg == "-f" and argv[i + 1].endswith(".compose.yaml") and "/ax-ipam-" in argv[i + 1]:
            with open(argv[i + 1]) as f:
                overlay = yaml.safe_load(f) or {}
            for name, value in overlay.get("networks", {}).items():
                networks[name]["ipam"]["config"][0].update(value["ipam"]["config"][0])
    print(json.dumps({"name": "ax", "networks": networks, "services": {}}))
    sys.exit(0)

if argv[:2] == ["docker", "compose"]:
    if "ps" in argv and "-q" in argv:
        print("cid-" + argv[-1])
    elif "ps" in argv:
        emit("FAKE_PS_OUT")
    elif "--services" in argv:
        emit("FAKE_SERVICES")
    elif "up" in argv:
        capture = os.environ.get("FAKE_COMPOSE_CAPTURE")
        if capture:
            import subprocess
            pos = argv.index("up")
            result = subprocess.run(argv[:pos] + ["--profile", "*", "config", "--format", "json"], capture_output=True, text=True, env={**os.environ, "DOCKER_HOST": "unix:///nonexistent"})
            if result.returncode:
                sys.exit(result.returncode)
            with open(capture, "w") as stream:
                stream.write(result.stdout)
        sys.exit(int(os.environ.get("FAKE_UP_RC", "0")))
    sys.exit(0)
if argv[:2] == ["docker", "inspect"]:
    print(os.environ.get("FAKE_INSPECT", "healthy"))
    sys.exit(0)
sys.exit(0)

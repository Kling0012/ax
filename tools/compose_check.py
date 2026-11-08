#!/usr/bin/env python3
"""compose の検査（F-14.1 の(3)、D-14、T-119）。標準ライブラリだけ。YAML は読まない。

入力は `docker compose config --format json` の出力（標準入力かファイル）。
自作の YAML 走査は使わない（`${…}` の展開と複数ファイルの重ね合わせは compose config に任せる。F-14.1）。

検査（(a) 終了コードと (b) 標準エラーは tools/ci.sh が行う。ここでは (c)〜(e) と網）:
  (c) 全サービスの ports で host_ip が空、0.0.0.0、:: のものが無い
  (d) compose.d/sl-*.yaml 由来のサービス（名前が sl- で始まる）に ports が無い
  (e) volumes に /var/run/docker.sock と /run/docker.sock が無い
  (f) gate 以外の ports の増加は、--allow-ports で許したサービス名だけ（D-14: gate 以外の ports が増えていないこと）
  (g) gate は ax-core の網だけに入る（D-14）
  (h) --require-networks: ax-core / ax-work / ax-job の ipam・bridge 名が F-18.1 のとおり（compose.yaml が使われているとき）

使い方: tools/compose_check.py [--allow-ports=gate,hq,...] [--no-network-check] [FILE|-]
終了コード: 0 合格 / 1 不合格 / 2 入力が読めない
"""
import json
import os
import sys

FORBIDDEN_HOST_IPS = {"", "0.0.0.0", "::", "[::]", None}
DOCKER_SOCKS = {"/var/run/docker.sock", "/run/docker.sock"}
EXPECTED_NETWORKS = {
    "ax-core": {"subnet": os.environ.get("AX_CORE_SUBNET", "192.0.2.0/25"), "bridge": "ax-core", "internal": False},
    "ax-work": {"subnet": os.environ.get("AX_WORK_SUBNET", "198.51.100.0/25"), "bridge": "ax-work", "internal": False},
    "ax-job": {"subnet": os.environ.get("AX_JOB_SUBNET", "203.0.113.0/25"), "bridge": "ax-job", "internal": True},
}
DEFAULT_PORT_SERVICES = {"gate"}


def check(model, allow_ports=frozenset(DEFAULT_PORT_SERVICES), network_check=True):
    """model は config --format json を読んだ dict。問題の文字列の一覧を返す。"""
    bad = []
    services = model.get("services") or {}
    for name, svc in sorted(services.items()):
        ports = svc.get("ports") or []
        for p in ports:
            if isinstance(p, str):  # 念のため（json では dict になる）
                bad.append(f"{name}: ports が文字列のまま: {p}")
                continue
            if p.get("host_ip") in FORBIDDEN_HOST_IPS:
                bad.append(f"{name}: ports に host_ip が無い、または 0.0.0.0/:: がある（published={p.get('published')}）")
        if name.startswith("sl-") and ports:
            bad.append(f"{name}: サブループ(sl-*)に ports がある（D-14）")
        if ports and name not in allow_ports and not name.startswith("sl-"):
            bad.append(f"{name}: 許可していないサービスに ports がある（許可: {sorted(allow_ports)}）")
        for v in svc.get("volumes") or []:
            src = v.get("source") if isinstance(v, dict) else str(v).split(":")[0]
            if src in DOCKER_SOCKS:
                bad.append(f"{name}: volumes に docker.sock がある（S-11.6）")
        nets = svc.get("networks") or {}
        netnames = list(nets.keys()) if isinstance(nets, dict) else list(nets)
        if name == "gate" and set(netnames) != {"ax-core"}:
            bad.append(f"gate: 網は ax-core だけ（実際: {sorted(netnames)}。D-14、F-18.4）")
    if network_check and model.get("networks"):
        for key, exp in EXPECTED_NETWORKS.items():
            net = model["networks"].get(key)
            if net is None:
                continue  # 使われていない網は config の出力に出ない
            bridge = (net.get("driver_opts") or {}).get("com.docker.network.bridge.name")
            if bridge != exp["bridge"]:
                bad.append(f"networks.{key}: bridge 名が {bridge!r}（期待 {exp['bridge']!r}）")
            cfgs = ((net.get("ipam") or {}).get("config")) or []
            if not any(c.get("subnet") == exp["subnet"] for c in cfgs):
                bad.append(f"networks.{key}: subnet が {exp['subnet']} でない")
            if bool(net.get("internal")) != exp["internal"]:
                bad.append(f"networks.{key}: internal が {exp['internal']} でない")
    return bad


def main(argv):
    allow = set(DEFAULT_PORT_SERVICES)
    netcheck = True
    src = "-"
    for a in argv[1:]:
        if a.startswith("--allow-ports="):
            allow = {x for x in a.split("=", 1)[1].split(",") if x}
        elif a == "--no-network-check":
            netcheck = False
        else:
            src = a
    try:
        text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
        model = json.loads(text)
    except (OSError, ValueError) as e:
        print(f"compose_check: 入力が読めない: {e}", file=sys.stderr)
        return 2
    bad = check(model, frozenset(allow), netcheck)
    for b in bad:
        print("NG:", b)
    if bad:
        return 1
    print("OK: compose の検査（ports、docker.sock、網）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

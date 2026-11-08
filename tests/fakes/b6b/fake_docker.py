#!/usr/bin/env python3
"""偽の docker（B6b の試験用）。ネットワークもデーモンも使わない。状態は FAKE_DOCKER_DIR/state.json、呼び出しは calls.jsonl。

対応するサブコマンド: run -d、ps（-a、-q、--filter、--format）、inspect --type container、stop、rm、logs。
試験の操作（環境変数）:
  FAKE_DOCKER_FAIL_RUN=1          docker run を失敗させる
  FAKE_DOCKER_BREAK=privileged|rootfs|network|socket|capadd   run の結果の隔離を壊して見せる（launcher が起動を止めるかを試す）
試験側の補助は同じファイルの FakeDocker クラス（import して使う）。
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path

VALUE_OPTS = {"--name", "--label", "--user", "--tmpfs", "--cap-drop", "--security-opt", "--pids-limit", "--memory", "--cpus",
              "--network", "--network-alias", "--dns", "--sysctl", "--entrypoint", "--subnet", "--gateway", "--ip", "--alias", "-v", "-w", "-e", "--filter", "--format", "-t", "--type"}


class FakeDocker:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.calls_path = self.root / "calls.jsonl"

    # --- 状態 ---
    def load(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"containers": {}}

    def save(self, st: dict) -> None:
        self.state_path.write_text(json.dumps(st))

    def calls(self) -> list[dict]:
        if not self.calls_path.exists():
            return []
        return [json.loads(x) for x in self.calls_path.read_text().splitlines() if x.strip()]

    def container(self, name: str) -> dict | None:
        return self.load()["containers"].get(name)

    def finish(self, name: str, exit_code: int = 0, logs: str = "") -> None:
        st = self.load()
        c = st["containers"][name]
        c["State"].update({"Status": "exited", "Running": False, "ExitCode": exit_code, "FinishedAt": "2026-11-07T00:00:00Z"})
        c["logs"] = logs
        self.save(st)

    def add_running(self, name: str, labels: dict) -> None:
        st = self.load()
        st["containers"][name] = {"Name": "/" + name, "Config": {"nodedels": labels}, "State": {"Status": "running", "Running": True, "ExitCode": 0},
                                  "HostConfig": {"Privileged": False, "CapAdd": None, "ReadonlyRootfs": True, "NetworkMode": "ax-job"},
                                  "Mounts": [], "logs": ""}
        self.save(st)

    def env(self) -> dict:
        return {"FAKE_DOCKER_DIR": str(self.root)}

    # --- コマンド ---
    def log_call(self, argv: list[str], environ: dict) -> None:
        with self.calls_path.open("a") as f:
            f.write(json.dumps({"argv": argv, "has_token": "ANTHROPIC_AUTH_TOKEN" in environ,
                                "token_len": len(environ.get("ANTHROPIC_AUTH_TOKEN", ""))}) + "\n")

    def main(self, argv: list[str], environ: dict) -> int:
        self.log_call(argv, environ)
        if not argv:
            return 1
        sub, args = argv[0], argv[1:]
        return getattr(self, "cmd_" + sub, self.cmd_unknown)(args, environ)

    def cmd_create(self, args, environ):
        result = self.cmd_run(args, environ)
        if result:
            return result
        opts, _ = self._parse(args)
        name = next(value for key, value in opts if key == "--name")
        state = self.load()
        state["containers"][name]["State"].update({"Status": "created", "Running": False})
        self.save(state)
        return 0

    def cmd_start(self, args, environ):
        state = self.load()
        name = args[0] if args else ""
        if name not in state["containers"]:
            return 1
        state["containers"][name]["State"].update({"Status": "running", "Running": True})
        self.save(state)
        print(name)
        return 0

    def cmd_unknown(self, args, environ):
        print("fake docker: 未対応のサブコマンド", file=sys.stderr)
        return 1

    @staticmethod
    def _parse(args: list[str]):
        """オプションと位置引数に分ける。(opts: list[(name,value)], rest: list)"""
        opts, rest, i = [], [], 0
        while i < len(args):
            a = args[i]
            if rest:  # イメージ（位置引数）より後は、コマンドの語としてそのまま受ける
                rest.append(a)
                i += 1
                continue
            if a in VALUE_OPTS and i + 1 < len(args):
                opts.append((a, args[i + 1]))
                i += 2
            elif a.startswith("-") and a not in ("-",) and not rest:
                opts.append((a, None))
                i += 1
            else:
                rest.append(a)
                i += 1
        return opts, rest

    def cmd_run(self, args, environ):
        if environ.get("FAKE_DOCKER_FAIL_RUN"):
            print("fake docker: run を失敗させる", file=sys.stderr)
            return 125
        opts, rest = self._parse(args)
        o = {}
        for k, v in opts:
            o.setdefault(k, []).append(v)
        name = o["--name"][0]
        st = self.load()
        if name in st["containers"]:
            print("fake docker: 名前が重複", file=sys.stderr)
            return 125
        labels = dict(x.split("=", 1) for x in o.get("--label", []))
        mounts, binds = [], []
        for v in o.get("-v", []):
            parts = v.split(":")
            src, dst = parts[0], parts[1]
            mode = parts[2] if len(parts) > 2 else "rw"
            mounts.append({"Type": "bind", "Source": src, "Destination": dst, "RW": mode == "rw", "Mode": mode})
            binds.append(v)
        env = [v if "=" in v else v + "=<redacted>" for v in o.get("-e", [])]
        brk = environ.get("FAKE_DOCKER_BREAK", "")
        host = {
            "Privileged": brk == "privileged", "CapAdd": ["SYS_ADMIN"] if brk == "capadd" else None, "CapDrop": o.get("--cap-drop"),
            "ReadonlyRootfs": brk != "rootfs" and ("--read-only" in [k for k, _ in opts]),
            "NetworkMode": "bridge" if brk == "network" else (o.get("--network") or ["default"])[0],
            "Binds": binds, "Tmpfs": {t.split(":")[0]: t for t in o.get("--tmpfs", [])},
            "PidsLimit": int(o.get("--pids-limit", ["0"])[0]), "Memory": o.get("--memory", [""])[0], "NanoCpus": o.get("--cpus", [""])[0],
            "SecurityOpt": o.get("--security-opt"), "Dns": o.get("--dns", []),
            "Sysctls": dict(value.split("=", 1) for value in o.get("--sysctl", [])),
        }
        if brk == "socket":
            mounts.append({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True, "Mode": "rw"})
        st["containers"][name] = {
            "Name": "/" + name, "Image": rest[0] if rest else "", "Cmd": rest[1:], "User": o.get("--user", [""])[0],
            "Config": {"nodedels": labels, "Env": env, "User": o.get("--user", [""])[0], "Entrypoint": [o["--entrypoint"][0]] if o.get("--entrypoint") else None, "WorkingDir": (o.get("-w") or [""])[0], "Image": rest[0] if rest else "", "Cmd": rest[1:]},
            "State": {"Status": "running", "Running": True, "ExitCode": 0, "StartedAt": "2026-11-07T00:00:00Z"},
            "HostConfig": host, "NetworkSettings": {"Networks": {}}, "Mounts": mounts, "logs": "", "run_argv": ["run"] + args,
        }
        self.save(st)
        primary_network = (o.get("--network") or [""])[0]
        if primary_network in st.get("networks", {}):
            aliases = []
            for alias in o.get("--network-alias", []):
                aliases.extend(["--alias", alias])
            if self.cmd_network(["connect", *aliases, primary_network, name], environ):
                return 125
        print("c0ffee" + str(len(st["containers"])))
        return 0

    def cmd_network(self, args, environ):
        if not args:
            return 1
        sub, argv = args[0], args[1:]
        opts, rest = self._parse(argv)
        options = {}
        for key, value in opts:
            options.setdefault(key, []).append(value)
        state = self.load()
        networks = state.setdefault("networks", {})
        if sub == "create" and len(rest) == 1:
            name = rest[0]
            if name in networks:
                return 1
            subnet = options.get("--subnet", ["172.31.0.0/24"])[0]
            try:
                net = ipaddress.ip_network(subnet, strict=False)
                gateway = options.get("--gateway", [str(next(net.hosts()))])[0]
                if ipaddress.ip_address(gateway) not in net:
                    return 1
            except (ValueError, StopIteration):
                return 1
            labels = {}
            for value in options.get("--label", []):
                key, _, val = value.partition("=")
                labels[key] = val
            networks[name] = {"Name": name, "Id": "id-" + name,
                "Driver": "bridge", "Internal": "--internal" in options,
                "nodedels": labels, "IPAM": {"Config": [{"Subnet": str(net), "Gateway": gateway}]},
                "Containers": {}}
            self.save(state)
            print("id-" + name)
            return 0
        if sub == "inspect" and len(rest) == 1:
            network = networks.get(rest[0])
            if network is None:
                return 1
            print(json.dumps([network]))
            return 0
        if sub == "connect" and len(rest) == 2:
            name, container_name = rest
            network, container = networks.get(name), state["containers"].get(container_name)
            if network is None or container is None or container_name in network["Containers"]:
                return 1
            config = network["IPAM"]["Config"][0]
            net = ipaddress.ip_network(config["Subnet"])
            occupied = {entry["IPv4Address"].split("/")[0] for entry in network["Containers"].values()}
            address = options.get("--ip", [None])[0]
            if address is None:
                address = next((str(ip) for ip in net.hosts() if str(ip) != config["Gateway"] and str(ip) not in occupied), None)
            try:
                if address is None or ipaddress.ip_address(address) not in net or address in occupied or address == config["Gateway"]:
                    return 1
            except ValueError:
                return 1
            network["Containers"][container_name] = {"Name": container_name, "IPv4Address": address + "/" + str(net.prefixlen)}
            container.setdefault("NetworkSettings", {}).setdefault("Networks", {})[name] = {
                "NetworkID": network["Id"], "IPAddress": address, "Gateway": config["Gateway"],
                "Aliases": options.get("--alias", [])}
            self.save(state)
            return 0
        return 1

    def cmd_exec(self, args, environ):
        if len(args) < 3:
            return 1
        container = self.load()["containers"].get(args[0])
        if not container or not container.get("State", {}).get("Running"):
            return 1
        commands = {
            ("sysctl", "-n", "net.ipv4.ip_forward"): "net.ipv4.ip_forward",
            ("cat", "/proc/sys/net/ipv4/ip_forward"): "net.ipv4.ip_forward",
            ("cat", "/proc/sys/net/ipv6/conf/all/forwarding"): "net.ipv6.conf.all.forwarding",
        }
        key = commands.get(tuple(args[1:]))
        if key:
            print(container.get("HostConfig", {}).get("Sysctls", {}).get(key, "0"))
            return 0
        return 1

    def cmd_ps(self, args, environ):
        opts, _ = self._parse(args)
        flags = {k for k, _ in opts}
        filters = [v for k, v in opts if k == "--filter"]
        fmt = next((v for k, v in opts if k == "--format"), None)
        st = self.load()
        out = []
        for name, c in sorted(st["containers"].items()):
            ok = True
            for f in filters:
                key, _, val = f.partition("=")
                if key == "label":
                    k2, _, v2 = val.partition("=")
                    noded = c["Config"]["nodedels"]
                    if k2 not in noded or (v2 and noded[k2] != v2):
                        ok = False
                elif key == "status":
                    if c["State"]["Status"] != val:
                        ok = False
            if not ok:
                continue
            if "-a" not in flags and c["State"]["Status"] != "running":
                continue
            if "-q" in flags:
                out.append("id-" + name)
            elif fmt:
                line = fmt.replace("{{.Names}}", name).replace("{{.State}}", c["State"]["Status"]).replace("{{.Status}}", c["State"]["Status"]).replace("\\t", "\t")
                out.append(line)
            else:
                out.append(name)
        if out:
            print("\n".join(out))
        return 0

    def cmd_inspect(self, args, environ):
        opts, rest = self._parse(args)
        c = self.load()["containers"].get(rest[-1] if rest else "")
        if c is None:
            print("Error: No such container", file=sys.stderr)
            return 1
        print(json.dumps([{k: v for k, v in c.items() if k not in ("logs",)}]))
        return 0

    def cmd_stop(self, args, environ):
        opts, rest = self._parse(args)
        st = self.load()
        c = st["containers"].get(rest[-1])
        if c is None:
            return 1
        c["State"].update({"Status": "exited", "Running": False, "ExitCode": 143, "FinishedAt": "2026-11-07T00:00:00Z"})
        self.save(st)
        print(rest[-1])
        return 0

    def cmd_rm(self, args, environ):
        opts, rest = self._parse(args)
        st = self.load()
        if rest[-1] not in st["containers"]:
            return 1
        del st["containers"][rest[-1]]  # 偽の状態の中だけ。実物の削除ではない
        self.save(st)
        print(rest[-1])
        return 0

    def cmd_logs(self, args, environ):
        opts, rest = self._parse(args)
        c = self.load()["containers"].get(rest[-1])
        if c is None:
            return 1
        sys.stdout.write(c.get("logs", ""))
        return 0


if __name__ == "__main__":
    sys.exit(FakeDocker(os.environ["FAKE_DOCKER_DIR"]).main(sys.argv[1:], dict(os.environ)))

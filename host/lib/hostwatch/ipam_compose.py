"""Validate Compose IPAM and add dynamic pools before invoking Compose."""
from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

NETWORKS = ("ax-core", "ax-work", "ax-publish-review", "ax-job")


class IPAMError(Exception):
    pass


def config(prefix, overlay=None):
    args = list(prefix)
    if overlay:
        args += ["-f", str(overlay)]
    args += ["--profile", "*", "config", "--format", "json"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode:
            raise IPAMError("Compose 設定の合成に失敗しました")
        return json.loads(result.stdout)
    except (OSError, ValueError) as exc:
        raise IPAMError("Compose 設定を読み取れません") from exc


def inspect(model):
    networks = {}
    for name in NETWORKS:
        if model.get("networks", {}).get(name, {}).get("external"):
            continue
        try:
            entries = model["networks"][name]["ipam"]["config"]
            if len(entries) != 1:
                raise ValueError("multiple subnets")
            item = entries[0]
            subnet = ipaddress.ip_network(item["subnet"], strict=True)
            gateway = ipaddress.ip_address(item["gateway"])
            if subnet.version != 4 or gateway not in subnet or gateway in (subnet.network_address, subnet.broadcast_address):
                raise ValueError("invalid gateway")
            pool = ipaddress.ip_network(item["ip_range"], strict=True) if item.get("ip_range") else None
            if pool and (pool.version != 4 or not pool.subnet_of(subnet)):
                raise ValueError("invalid pool")
            networks[name] = (subnet, gateway, pool, set())
        except (KeyError, TypeError, ValueError) as exc:
            raise IPAMError(f"{name}: subnet、gateway または ip_range が不正です") from exc
    for service in model.get("services", {}).values():
        for name, settings in (service.get("networks") or {}).items():
            if name not in networks or not settings or not settings.get("ipv4_address"):
                continue
            subnet, gateway, pool, fixed = networks[name]
            try:
                address = ipaddress.ip_address(settings["ipv4_address"])
                if address not in subnet or address in (gateway, subnet.network_address, subnet.broadcast_address) or address in fixed:
                    raise ValueError("invalid fixed address")
                fixed.add(address)
            except ValueError as exc:
                raise IPAMError(f"{name}: 固定 IP が不正または重複しています") from exc
    for name, (subnet, gateway, pool, fixed) in networks.items():
        if pool and any(address in pool for address in fixed | {gateway}):
            raise IPAMError(f"{name}: ip_range が gateway または固定 IP と重なります")
    return networks


def select(subnet, excluded):
    candidates = [subnet]
    for address in sorted(excluded):
        host = ipaddress.ip_network(f"{address}/32")
        candidates = [part for candidate in candidates for part in
                      (list(candidate.address_exclude(host)) if address in candidate else [candidate])]
    candidates = [candidate for candidate in candidates if candidate.num_addresses > 1 or
                  candidate.network_address not in (subnet.network_address, subnet.broadcast_address)]
    if not candidates:
        raise IPAMError("動的割当範囲を確保できません")
    return min(candidates, key=lambda net: (net.prefixlen, int(net.network_address)))


class Reset:
    pass


class OverlayDumper(yaml.SafeDumper):
    pass


OverlayDumper.add_representer(Reset, lambda dumper, _: dumper.represent_scalar("!reset", "null"))


def existing_networks(prefix, model, networks):
    try:
        index = prefix.index("compose")
        docker = prefix[:index]
        if not docker or Path(docker[-1]).name != "docker":
            raise IPAMError("Docker コマンドを特定できません")
        listing = subprocess.run([*docker, "network", "ls", "--format", "{{.Name}}"], capture_output=True, text=True)
        if listing.returncode:
            raise IPAMError("既存ネットワークを列挙できません")
        names = set(listing.stdout.splitlines())
        existing = set()
        for name, (subnet, gateway, pool, _) in networks.items():
            actual_name = model["networks"][name]["name"]
            if actual_name not in names:
                continue
            result = subprocess.run([*docker, "network", "inspect", actual_name], capture_output=True, text=True)
            if result.returncode:
                raise IPAMError(f"{name}: 既存ネットワークを検査できません")
            data = json.loads(result.stdout)
            if len(data) != 1 or data[0].get("Name") != actual_name:
                raise IPAMError(f"{name}: 既存ネットワークの名前が一致しません")
            labels = data[0].get("nodedels") or {}
            if labels.get("com.docker.compose.project") != model["name"] or labels.get("com.docker.compose.network") != name:
                raise IPAMError(f"{name}: 既存ネットワークの所有ラベルが一致しません")
            configured = model["networks"][name]
            actual = data[0]
            for compose_key, docker_key in (("driver", "Driver"), ("internal", "Internal"),
                                            ("attachable", "Attachable"), ("enable_ipv6", "EnableIPv6")):
                if compose_key in configured and configured[compose_key] != actual.get(docker_key):
                    raise IPAMError(f"{name}: 既存ネットワークの {compose_key} が一致しません")
            if any((actual.get("Options") or {}).get(k) != v for k, v in (configured.get("driver_opts") or {}).items()):
                raise IPAMError(f"{name}: 既存ネットワークの driver_opts が一致しません")
            if any(labels.get(k) != v for k, v in (configured.get("labels") or {}).items()):
                raise IPAMError(f"{name}: 既存ネットワークの labels が一致しません")
            ipam = configured.get("ipam", {})
            if ipam.get("driver") and ipam["driver"] != actual["IPAM"].get("Driver"):
                raise IPAMError(f"{name}: 既存ネットワークの IPAM driver が一致しません")
            if any((actual["IPAM"].get("Options") or {}).get(k) != v for k, v in (ipam.get("options") or {}).items()):
                raise IPAMError(f"{name}: 既存ネットワークの IPAM options が一致しません")
            configs = actual["IPAM"]["Config"]
            if len(configs) != 1:
                raise IPAMError(f"{name}: 既存ネットワークの IPAM が不正です")
            item = configs[0]
            if ipaddress.ip_network(item["Subnet"]) != subnet or ipaddress.ip_address(item["Gateway"]) != gateway:
                raise IPAMError(f"{name}: 既存ネットワークの subnet または gateway が一致しません")
            actual_pool = ipaddress.ip_network(item["IPRange"]) if item.get("IPRange") else None
            if pool is not None and pool != actual_pool:
                raise IPAMError(f"{name}: 明示された ip_range が既存ネットワークと一致しません")
            existing.add(name)
        return existing
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise IPAMError("既存ネットワークを検査できません") from exc


def prepare(prefix, *, protect_existing=False):
    first = config(prefix)
    before = inspect(first)
    existing = existing_networks(prefix, first, before) if protect_existing else set()
    additions = {}
    expected = {}
    for name, (subnet, gateway, pool, fixed) in before.items():
        if name in existing:
            original = first["networks"][name]
            additions[name] = {key: Reset() for key in original if key not in ("name", "external")}
            additions[name].update({"name": original["name"].replace("$", "$$"), "external": True})
            continue
        if pool is None:
            pool = select(subnet, fixed | {gateway})
            additions[name] = {"ipam": {"config": [{"subnet": str(subnet), "ip_range": str(pool)}]}}
        expected[name] = pool
    tmpdir = os.environ.get("TMPDIR")
    if not tmpdir:
        raise IPAMError("TMPDIR が設定されていません")
    directory = Path(tmpdir) / f"ax-ipam-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise IPAMError("IPAM の一時領域の所有者または権限が不正です")
    content = yaml.dump({"networks": additions}, Dumper=OverlayDumper, sort_keys=True) if additions else "{}\n"
    suffix = hashlib.blake2s(content.encode(), digest_size=16).hexdigest()
    overlay = directory / (suffix + ".compose.yaml")
    if not overlay.exists():
        fd, temp_path = tempfile.mkstemp(prefix="candidate-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        try:
            os.link(temp_path, overlay)
        except FileExistsError:
            pass
    info = overlay.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or overlay.read_text(encoding="utf-8") != content:
        raise IPAMError("IPAM の一時ファイルを検証できません")
    final = config(prefix, overlay)
    after = inspect(final)
    if final.get("services") != first.get("services"):
        raise IPAMError("最終 Compose 設定のサービスが変わりました")
    for name, (subnet, gateway, _, fixed) in before.items():
        if name in existing:
            net = final["networks"][name]
            if not net.get("external") or net.get("name") != first["networks"][name]["name"] or net.get("ipam", {}).get("config"):
                raise IPAMError(f"{name}: 外部ネットワークの参照が不正です")
            continue
        final_subnet, final_gateway, final_pool, final_fixed = after[name]
        if (subnet, gateway, fixed) != (final_subnet, final_gateway, final_fixed) or final_pool != expected[name]:
            raise IPAMError(f"{name}: 最終 Compose 設定が初回と一致しません")
    for name in NETWORKS:
        if first["networks"][name].get("external") and final["networks"][name] != first["networks"][name]:
            raise IPAMError(f"{name}: 外部ネットワークの設定が変わりました")
    return overlay


def main():
    try:
        separator = sys.argv.index("--", 1)
        prefix, tail = sys.argv[1:separator], sys.argv[separator + 1:]
        if not prefix or not tail:
            raise IPAMError("Compose 引数が不足しています")
        overlay = prepare(prefix, protect_existing=tail[0] in ("up", "create", "run", "ax-ipam-check"))
        if tail == ["ax-ipam-check"]:
            return 0
        command = prefix + (["-f", str(overlay)] if overlay else []) + tail
        os.execvp(command[0], command)
    except (IPAMError, ValueError, OSError) as exc:
        print(f"ax-ipam: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""本机网络信息（v1.1 配对 URI 的 ``hosts=``/``name=`` 候选，D-031）。

- ``collect_private_ipv4()`` 返回可用的私网 IPv4（10/8、172.16/12、192.168/16），
  排除回环、链路本地、198.18/198.19，以及常见虚拟网卡；
- 排序：WLAN/无线优先 → 以太网 → 其它，虚拟网卡不参与；
- ``pairing_hosts()`` 生成配对 URI 的 ``hosts=`` 列表（host 本身去重后放最前）；
- ``computer_name()`` 返回电脑显示名（配对 URI 的 ``name=``）。

可用环境变量 ``DSH_PHONE_PAIR_HOSTS``（逗号分隔）覆盖，便于测试/特殊部署。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
from typing import Iterable

logger = logging.getLogger("bridge.netinfo")

ENV_PAIR_HOSTS = "DSH_PHONE_PAIR_HOSTS"

#: 虚拟网卡/隧道关键字（适配器名含其中之一则不参与配对候选）。
VIRTUAL_HINTS = (
    "vmware",
    "virtualbox",
    "vbox",
    "hyper-v",
    "vethernet",
    "mihomo",
    "clash",
    "tailscale",
    "zerotier",
    "wsl",
    "docker",
    "br-",
    "loopback",
    "bluetooth",
    "tap",
    "tun",
    "vpn",
)
WIRELESS_HINTS = ("wlan", "wi-fi", "wifi", "无线", "wlp")
ETHERNET_HINTS = ("ethernet", "以太网", "eth", "enp", "本地连接")

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

#: RFC 1918 私网段。注意：``ipaddress.IPv4Address.is_private`` 在 Python 3.11
#: 里还把 240/4（含 255.255.0.0 这类子网掩码）算作 private，不能直接用。
_PRIVATE_NETWORKS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)


def is_private_ipv4(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    if address.is_loopback or address.is_link_local or address.is_multicast:
        return False
    if address.is_unspecified:
        return False
    if str(address).startswith(("198.18.", "198.19.")):
        return False
    return any(address in network for network in _PRIVATE_NETWORKS)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def _from_ipconfig() -> list[tuple[str, str]]:
    """解析 ``ipconfig`` 输出 → [(适配器名, IPv4)]（Windows）。"""
    if sys.platform != "win32":
        return []
    try:
        proc = subprocess.run(
            ["ipconfig"],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 环境差异
        return []
    text = _decode(proc.stdout)
    results: list[tuple[str, str]] = []
    adapter = ""
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            adapter = line.strip().rstrip(":").strip()
            continue
        if adapter and _IPV4_RE.search(line):
            for match in _IPV4_RE.findall(line):
                results.append((adapter, match))
    return results


def _from_socket() -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            results.append((hostname, address))
    except OSError:  # pragma: no cover - 环境差异
        return []
    return results


def _score(adapter: str) -> int:
    lower = adapter.lower()
    score = 0
    if any(hint in lower for hint in VIRTUAL_HINTS):
        score -= 100
    if any(hint in lower for hint in WIRELESS_HINTS):
        score += 100
    elif any(hint in lower for hint in ETHERNET_HINTS):
        score += 20
    return score


def collect_candidates() -> list[dict[str, object]]:
    """返回私网 IPv4 候选详情（adapter/ip/score），已排序去重。"""
    entries: list[tuple[str, str]] = []
    entries.extend(_from_ipconfig())
    if not entries:
        entries.extend(_from_socket())
    ranked: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for adapter, address in entries:
        if address in seen or not is_private_ipv4(address):
            continue
        score = _score(adapter)
        if score < 0:  # D-031：虚拟网卡/隧道不参与配对候选
            continue
        seen.add(address)
        ranked.append((score, adapter, address))
    ranked.sort(key=lambda item: (-item[0], item[2]))
    return [
        {"adapter": adapter, "ip": address, "score": score}
        for score, adapter, address in ranked
    ]


def collect_private_ipv4() -> list[str]:
    """返回私网 IPv4 候选（已排序去重；不含 host 参数）。"""
    return [str(item["ip"]) for item in collect_candidates()]


def pairing_hosts(listen_host: str) -> list[str]:
    """生成 ``hosts=`` 候选：host 本身在最前，其余私网地址按优先级排列。"""
    override = os.environ.get(ENV_PAIR_HOSTS, "").strip()
    if override:
        values = [item.strip() for item in override.split(",") if item.strip()]
    else:
        values = collect_private_ipv4()
    result: list[str] = []
    host = (listen_host or "").strip()
    if is_private_ipv4(host):
        result.append(host)
    elif host and host not in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        result.append(host)
    for address in values:
        if address not in result:
            result.append(address)
    return result


def computer_name() -> str:
    """电脑显示名（配对 URI 的 ``name=``）；缺失时返回空串。"""
    for env_name in ("COMPUTERNAME", "HOSTNAME"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    try:
        return socket.gethostname().strip()
    except OSError:  # pragma: no cover - 环境差异
        return ""


__all__: Iterable[str] = (
    "collect_private_ipv4",
    "computer_name",
    "is_private_ipv4",
    "pairing_hosts",
)

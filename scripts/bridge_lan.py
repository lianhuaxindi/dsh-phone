"""Detect the LAN IPv4 address the phone should connect to.

Usage:
    python scripts/bridge_lan.py            # human readable (IP + candidates)
    python scripts/bridge_lan.py --quiet    # only the best IP on stdout
    python scripts/bridge_lan.py --json     # {"best": "...", "candidates": [...]}

Selection rules (in order):
  1. Skip loopback / APIPA (169.254.) / proxy fake-IP ranges (198.18.*, 198.19.).
  2. Skip virtual adapters (VMware VMnet*, VirtualBox, Hyper-V, WSL, docker,
     Mihomo/Clash TUN, ...), because the phone cannot reach those.
  3. Prefer WLAN / Wi-Fi adapters, then private-range addresses
     (10/8, 172.16/12, 192.168/16), then wired Ethernet.

Exit codes: 0 = found, 1 = no usable address, 2 = usage error.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import subprocess
import sys

SKIP_PREFIXES = ("127.", "169.254.", "198.18.", "198.19.", "0.")
VIRTUAL_HINTS = (
    "vmnet",
    "vmware",
    "virtualbox",
    "hyper-v",
    "vethernet",
    "loopback",
    "mihomo",
    "clash",
    "tailscale",
    "zerotier",
    "wsl",
    "docker",
    "br-",
    "vbox",
)
WIRELESS_HINTS = ("wlan", "wi-fi", "wifi", "无线", "wireless")
ETHERNET_HINTS = ("ethernet", "以太网")

IPV4_RE = re.compile(r"IPv4[^:]*:\s*(\d{1,3}(?:\.\d{1,3}){3})")


def _decode(raw: bytes) -> str:
    """ipconfig output is encoded with the active console/ANSI codepage."""
    for enc in ("gbk", "utf-8", "mbcs", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


def _from_ipconfig() -> list[tuple[str, str]]:
    """Return [(adapter_name, ipv4)] parsed from `ipconfig` output."""
    try:
        raw = subprocess.run(
            ["ipconfig"], capture_output=True, timeout=20, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    if not raw:
        return []

    found: list[tuple[str, str]] = []
    adapter = ""
    for line in _decode(raw).splitlines():
        if line and not line[0].isspace():
            # adapter header, e.g. "Wireless LAN adapter WLAN:" or "以太网适配器 以太网:"
            adapter = line.strip().rstrip(":").strip()
            continue
        m = IPV4_RE.search(line)
        if m and adapter:
            found.append((adapter, m.group(1)))
    return found


def _from_socket() -> list[tuple[str, str]]:
    """Fallback: hostname resolution + default-route probe."""
    found: list[tuple[str, str]] = []
    try:
        for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET
        ):
            if family == socket.AF_INET:
                found.append(("hostname", sockaddr[0]))
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(1.0)
        probe.connect(("223.5.5.5", 53))
        found.append(("default-route", probe.getsockname()[0]))
        probe.close()
    except OSError:
        pass
    return found


def _is_usable(addr: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return False
    return not addr.startswith(SKIP_PREFIXES)


def _score(adapter: str, addr: str) -> int:
    low = (adapter or "").lower()
    if any(hint in low for hint in VIRTUAL_HINTS):
        return -1
    score = 0
    if any(hint in low for hint in WIRELESS_HINTS):
        score += 100
    elif any(hint in low for hint in ETHERNET_HINTS):
        score += 20
    if any(hint in low for hint in ("virtual", "pseudo", "假")):
        score -= 50
    try:
        if ipaddress.IPv4Address(addr).is_private:
            score += 10
    except ValueError:
        pass
    return score


def collect() -> list[dict[str, object]]:
    """Return usable candidate adapters, best first."""
    seen: dict[str, dict[str, object]] = {}
    for adapter, addr in _from_ipconfig() + _from_socket():
        if not _is_usable(addr):
            continue
        score = _score(adapter, addr)
        if score < 0:
            continue
        current = seen.get(addr)
        if current is None or score > int(current["score"]):
            seen[addr] = {"adapter": adapter, "ip": addr, "score": score}
    candidates = sorted(seen.values(), key=lambda item: int(item["score"]), reverse=True)
    return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect the LAN IPv4 for the DSH Phone Bridge.")
    parser.add_argument("--quiet", action="store_true", help="print only the best IP")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    candidates = collect()
    if not candidates:
        if not args.json and not args.quiet:
            print("未能自动检测局域网 IPv4。请用 ipconfig 查看 WLAN 的 IPv4 地址后手动指定。", file=sys.stderr)
        return 1

    best = str(candidates[0]["ip"])
    if args.json:
        print(json.dumps({"best": best, "candidates": candidates}, ensure_ascii=False))
    elif args.quiet:
        print(best)
    else:
        print(f"最佳局域网地址: {best}  (来自 {candidates[0]['adapter']})")
        others = [c for c in candidates[1:]]
        if others:
            print("其他候选:")
            for item in others:
                print(f"  - {item['ip']}  ({item['adapter']})")
        print()
        print("提示: 手机需与该地址同网段（同一 Wi-Fi / 局域网）才能连上 Bridge。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

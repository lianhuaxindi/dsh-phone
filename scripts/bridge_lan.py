"""Detect the LAN IPv4 address the phone should connect to.

Usage:
    python scripts/bridge_lan.py            # human readable (IP + candidates)
    python scripts/bridge_lan.py --quiet    # only the best IP on stdout
    python scripts/bridge_lan.py --json     # {"best": "...", "candidates": [...]}

Selection rules live in ``bridge/netinfo.py`` (single source of truth, D-031):
  1. Skip loopback / APIPA (169.254.) / proxy fake-IP ranges (198.18.*, 198.19.).
  2. Only RFC 1918 private ranges (10/8, 172.16/12, 192.168/16); this also
     filters subnet masks (e.g. 255.255.255.0) reported by ipconfig.
  3. Skip virtual adapters (VMware VMnet*, VirtualBox, Hyper-V, WSL, docker,
     Mihomo/Clash TUN, ...), because the phone cannot reach those.
  4. Prefer WLAN / Wi-Fi adapters, then Ethernet, then other adapters.

Exit codes: 0 = found, 1 = no usable address, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bridge.netinfo import computer_name, collect_candidates  # noqa: E402

BEST_ADAPTER_KEY = "adapter"


def collect() -> list[dict[str, object]]:
    """Return usable candidate adapters, best first (never raises)."""
    try:
        return collect_candidates()
    except Exception:  # pragma: no cover - 环境差异，脚本不应崩溃
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect the LAN IPv4 for the DSH Phone Bridge."
    )
    parser.add_argument("--quiet", action="store_true", help="print only the best IP")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    candidates = collect()
    if not candidates:
        if not args.json and not args.quiet:
            print(
                "未能自动检测局域网 IPv4。请用 ipconfig 查看 WLAN 的 IPv4 地址后手动指定。",
                file=sys.stderr,
            )
        return 1

    best = str(candidates[0]["ip"])
    if args.json:
        print(
            json.dumps(
                {
                    "best": best,
                    "computerName": computer_name(),
                    "candidates": candidates,
                },
                ensure_ascii=False,
            )
        )
    elif args.quiet:
        print(best)
    else:
        print(
            f"最佳局域网地址: {best}  (来自 {candidates[0][BEST_ADAPTER_KEY]})"
        )
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

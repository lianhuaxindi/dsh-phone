"""Download the deepseek-harness-sdk wheel from a PyPI mirror without pip's resolver.

pip's index/resolver path stalls on this machine's network, while plain urllib
works, so the spike bootstraps the wheel directly and installs it offline:

    python _bootstrap_wheel.py                 # newest release
    python _bootstrap_wheel.py 0.2.2rc2        # pinned release

Prints the resolved URL, sha256, wheel metadata (Requires-Dist) and the local
path, so the install step is reproducible and auditable.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import sysconfig
import urllib.parse
import urllib.request
import zipfile

MIRROR = os.environ.get("SPIKE_PYPI_MIRROR", "https://pypi.tuna.tsinghua.edu.cn/simple")
PACKAGE = os.environ.get("SPIKE_PACKAGE", "deepseek-harness-sdk")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


def _version_key(name: str) -> tuple:
    # wheel filename: {name}-{version}-{pytag}-{abitag}-{platformtag}.whl
    stem = name[: -len(".whl")]
    parts = stem.split("-")
    if len(parts) < 5:
        return (0,)
    return tuple(int(p) for p in re.findall(r"\d+", parts[1]))


def _version_of(name: str) -> str:
    return name[: -len(".whl")].split("-")[1]


def _platform_tag() -> str:
    """Native platform tag for this interpreter, e.g. 'win_amd64'."""
    return sysconfig.get_platform().replace("-", "_").replace(".", "_")


def main(argv: list[str]) -> int:
    wanted = argv[1] if len(argv) > 1 else None
    index_url = f"{MIRROR}/{PACKAGE}/"
    print(f"[1] index      : {index_url}")
    html = urllib.request.urlopen(index_url, timeout=90).read().decode("utf-8", "replace")

    links: dict[str, str] = {}
    for href in re.findall(r'href="([^"]+\.whl[^"]*)"', html):
        clean = urllib.parse.urljoin(index_url, href.split("#")[0])
        links[os.path.basename(urllib.parse.urlparse(clean).path)] = clean
    print(f"[2] wheels     : {len(links)} found")

    if not links:
        print("!! no wheels on this mirror")
        return 2

    if "--list" in argv:
        for name in sorted(links, key=_version_key):
            print(f"      {name}")
        return 0

    platform_tag = _platform_tag()
    if platform_tag:
        native = {n: u for n, u in links.items() if n.endswith(f"-{platform_tag}.whl")}
        if native:
            links = native
            print(f"[2b] platform  : kept {len(links)} wheel(s) for {platform_tag}")

    if wanted:
        matches = [n for n in links if f"-{wanted}-" in n]
        if not matches:
            print(f"!! version {wanted} not found; available: {sorted(links)}")
            return 2
        chosen = matches[0]
    else:
        chosen = max(sorted(links), key=_version_key)

    url = links[chosen]
    version = _version_of(chosen)
    os.makedirs(CACHE, exist_ok=True)
    dest = os.path.join(CACHE, chosen)

    if os.path.exists(dest):
        print(f"[3] cached     : {dest}")
    else:
        print(f"[3] downloading: {url}")
        data = urllib.request.urlopen(url, timeout=180).read()
        with open(dest, "wb") as fh:
            fh.write(data)
        print(f"[3] saved      : {dest} ({len(data)} bytes)")

    with open(dest, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    print(f"[4] sha256     : {digest}")
    print(f"[5] version    : {version}")
    print(f"[6] wheel      : {dest}")

    with zipfile.ZipFile(dest) as zf:
        meta_name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        meta = zf.read(meta_name).decode("utf-8", "replace")
        files = [n for n in zf.namelist() if n.endswith(".py")]
    print(f"[7] modules    : {len(files)}")
    for name in sorted(files):
        print(f"      {name}")
    print("[8] METADATA   :")
    for line in meta.splitlines():
        if line.startswith(("Name:", "Version:", "Requires-Python:", "Requires-Dist:", "Summary:")):
            print(f"      {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

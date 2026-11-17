"""Extract the spike wheels into a workspace-local vendor directory.

Why this exists: this session runs under a write sandbox that cannot modify the
conda env (``D:\\anaconda3\\envs\\scisim-agent``) and pip's own ``--target``
staging fails with WinError 5 inside the workspace temp dir. A wheel is just a
zip whose layout equals the install layout, so extracting it byte-for-byte is
equivalent for pure-Python wheels and keeps every artifact inside the repo.

    python _vendor_install.py            # extract both cached wheels
    python _vendor_install.py --clean    # wipe vendor dir first

Install target: scripts/spike/.vendor  (add it to PYTHONPATH or let
spike_sdk.py bootstrap sys.path itself).
"""

from __future__ import annotations

import glob
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
VENDOR = os.path.join(HERE, ".vendor")


def main(argv: list[str]) -> int:
    if "--clean" in argv and os.path.isdir(VENDOR):
        shutil.rmtree(VENDOR)
    os.makedirs(VENDOR, exist_ok=True)

    wheels = sorted(glob.glob(os.path.join(CACHE, "deepseek_harness*0.2.2rc2*.whl")))
    wheels = [w for w in wheels if "win_amd64" in w or "none-any" in w]
    if len(wheels) < 2:
        print(f"!! expected 2 wheels in {CACHE}, found {len(wheels)}: {wheels}")
        return 2

    for wheel in wheels:
        with zipfile.ZipFile(wheel) as zf:
            for info in zf.infolist():
                if info.filename.endswith("/"):
                    continue
                dest = os.path.join(VENDOR, info.filename)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        print(f"[extract] {os.path.basename(wheel)} -> {VENDOR}")

    for dist in sorted(glob.glob(os.path.join(VENDOR, "*.dist-info"))):
        print(f"[dist] {os.path.basename(dist)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

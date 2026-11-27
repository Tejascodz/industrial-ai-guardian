"""Download HAI 21.03 (about 180 MB) into data/hai/ and verify every file.

    python scripts/fetch_hai.py

HAI is published by the National Security Research Institute (Korea) at
https://github.com/icsdataset/hai under CC BY-SA 4.0. It is not redistributed
with this repository. Files are fetched from a pinned commit and checked
against the SHA-256 hashes in guardian/hai.py; a mismatch deletes the file.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardian import hai  # noqa: E402

COMMIT = "2a814cebc9a66b06c9e5cd545e2d72e65d383737"  # pragma: allowlist secret (git commit)
BASE = f"https://raw.githubusercontent.com/icsdataset/hai/{COMMIT}/hai-21.03/"


def main() -> None:
    hai.HAI_DIR.mkdir(parents=True, exist_ok=True)
    for name, digest in hai.SHA256.items():
        dest = hai.HAI_DIR / name
        if dest.exists() and hai.sha256(dest) == digest:
            print(f"ok      {name}")
            continue
        tmp = dest.with_suffix(".part")
        print(f"fetch   {name}")
        with urllib.request.urlopen(BASE + name, timeout=120) as r, tmp.open("wb") as f:  # noqa: S310  # nosec B310
            while chunk := r.read(1 << 20):
                f.write(chunk)
        if hai.sha256(tmp) != digest:
            tmp.unlink()
            sys.exit(f"{name}: SHA-256 mismatch, file deleted")
        tmp.replace(dest)
    print(f"HAI 21.03 ready in {hai.HAI_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

"""Fit Guardian, calibrate its alarm limits and save a signed model artifact.

    GUARDIAN_KEY=<secret> python scripts/export_model.py [--out models/guardian]

The key signs the artifact manifest (HMAC-SHA256). Keep it out of the repo,
e.g. in the host's secret store; anyone holding it can sign a model.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardian import artifacts  # noqa: E402
from guardian.evaluate import calibrate, prepare  # noqa: E402
from guardian.features import load_split, runs  # noqa: E402
from guardian.methods import Guardian  # noqa: E402
from guardian.sim import NORMAL  # noqa: E402


def signing_key() -> bytes:
    """The HMAC key, from the environment only. There is deliberately no default."""
    key = os.environ.get("GUARDIAN_KEY", "")
    if len(key) < 16:
        sys.exit("set GUARDIAN_KEY to a secret of at least 16 characters, e.g.\n"
                 '  export GUARDIAN_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")')
    return key.encode()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "models" / "guardian"))
    ap.add_argument("--target-fa", type=float, default=0.25)
    args = ap.parse_args()
    train, _ = load_split("train")
    val, vm = load_split("val")
    vr = list(runs(val))
    g = Guardian().fit([r for _, r in runs(train)], [r for n, r in vr if vm[n]["event_class"] == NORMAL])
    pv = prepare(g, vr, vm)
    lim, q = calibrate(pv, args.target_fa)
    out = artifacts.save(g, args.out, limits=lim, channels=pv[0].a.channels, key=signing_key())
    print(f"saved signed model to {out} (quantile {q:.5f})")


if __name__ == "__main__":
    main()

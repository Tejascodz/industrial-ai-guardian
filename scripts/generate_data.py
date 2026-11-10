"""Simulate every scenario and store one parquet file per split.

    python scripts/generate_data.py [--jobs N] [--only SPLIT ...]

Splits: train, val, test, val_attack, test_attack. The attack splits are
planned from the normal training history, so train is generated first.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from joblib import Parallel, delayed

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guardian.scenarios import all_splits, attack_splits  # noqa: E402
from guardian.sim import run  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"


def _one(s):
    df = run(s)
    return df, df.attrs.get("shutdown", "")


def _write(split: str, scenarios, jobs: int) -> None:
    t0 = time.time()
    res = Parallel(n_jobs=jobs)(delayed(_one)(s) for s in scenarios)
    frames, meta = [], []
    for s, (df, trip) in zip(scenarios, res):
        frames.append(df)
        m = asdict(s)
        m["description"] = s.describe()
        m["shutdown"] = trip
        meta.append(m)
    pd.concat(frames, ignore_index=True).to_parquet(DATA / f"{split}.parquet")
    (DATA / f"{split}_scenarios.json").write_text(json.dumps(meta, indent=1))
    trips = sum(1 for m in meta if m["shutdown"])
    print(f"{split}: {len(scenarios)} runs, {trips} plant trips, {time.time() - t0:.0f} s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=-1)
    ap.add_argument("--only", nargs="*", help="generate only these splits")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    wanted = lambda s: not args.only or s in args.only  # noqa: E731
    for split, scenarios in all_splits().items():
        if wanted(split):
            _write(split, scenarios, args.jobs)
    if any(wanted(s) for s in ("val_attack", "test_attack")):
        for split, scenarios in attack_splits().items():
            if wanted(split):
                _write(split, scenarios, args.jobs)


if __name__ == "__main__":
    main()

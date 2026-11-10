"""Benchmark all monitoring methods on the test split.

    python scripts/run_benchmark.py [--target-fa 0.25]

Writes results/benchmark_runs.csv (one row per method x test run) and
results/benchmark_summary.csv, results/operating_curve.csv (detection and
diagnosis at several false-alarm budgets) and results/attack_summary.csv
(integrity attacks, scored with the same thresholds).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardian.evaluate import calibrate, prepare, score, summarize, summarize_attacks  # noqa: E402
from guardian.features import load_split, runs  # noqa: E402
from guardian.methods import all_methods  # noqa: E402
from guardian.sim import NORMAL  # noqa: E402

RESULTS = ROOT / "results"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-fa", type=float, default=0.25, help="false alarms per day on validation")
    ap.add_argument("--only", default=None, help="run only methods whose name contains this text")
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)

    train, _ = load_split("train")
    val, val_meta = load_split("val")
    test, test_meta = load_split("test")
    attacks, attack_meta = load_split("test_attack")
    attack_runs = list(runs(attacks))
    train_runs = [g for _, g in runs(train)]
    val_runs = list(runs(val))
    test_runs = list(runs(test))
    val_normal = [g for n, g in val_runs if val_meta[n]["event_class"] == NORMAL]

    all_rows, summary, curve, attack_rows = [], [], [], []
    for method in all_methods():
        if args.only and args.only not in method.name:
            continue
        t0 = time.time()
        method.fit(train_runs, val_normal)
        pv = prepare(method, val_runs, val_meta)
        pt = prepare(method, test_runs, test_meta)
        lim, q = calibrate(pv, args.target_fa)
        df = score(method, pt, lim)
        if "w/o" not in method.name:  # operating curve: detection vs. false alarms
            for target in (0.05, 0.1, 0.25, 0.5, 1.0, 2.0):
                s = summarize(score(method, pt, calibrate(pv, target)[0]))
                curve.append({"method": method.name, "target_fa_val": target, **s})
        df.insert(0, "method", method.name)
        all_rows.append(df)
        da = score(method, prepare(method, attack_runs, attack_meta), lim)
        sa = summarize_attacks(da)
        sa.insert(0, "method", method.name)
        attack_rows.append(sa)
        if method.name == "Guardian":
            da.to_csv(RESULTS / "attack_runs_guardian.csv", index=False)
        for when, suffix in (("at detection", ""), ("1 h after detection", "_1h")):
            s = summarize(df, suffix)
            summary.append({"method": method.name, "diagnosis": when, "quantile": q, **s})
        print(f"{method.name}: {time.time() - t0:.0f} s, q={q:.5f}", flush=True)

    pd.concat(all_rows).to_csv(RESULTS / "benchmark_runs.csv", index=False)
    pd.DataFrame(curve).to_csv(RESULTS / "operating_curve.csv", index=False)
    am = pd.concat(attack_rows)
    am.to_csv(RESULTS / "attack_summary.csv", index=False)
    sm = pd.DataFrame(summary)
    sm.to_csv(RESULTS / "benchmark_summary.csv", index=False)
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(sm.drop(columns=["quantile"]).round(3).to_string(index=False))
        print(am.round(3).to_string(index=False))


if __name__ == "__main__":
    main()

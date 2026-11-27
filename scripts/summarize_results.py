"""Tables for the README, with 95 % confidence intervals, from the committed results.

    python scripts/summarize_results.py

Reads results/benchmark_runs.csv and results/attack_summary.csv (written by
run_benchmark.py) and writes:

    results/table_main.csv        per method: false alarms, detection, delay, diagnosis
    results/table_loops.csv       transmitter faults inside vs outside control loops
    results/table_fault_type.csv  transmitter named, by fault type
    results/table_paired.csv      Guardian vs each baseline on the same events (McNemar)
    results/guardian_misses.csv   every test event Guardian did not detect
    results/table_attacks.csv     per method: attacks detected, delay, recognised as attacks
    results/table_ablation.csv    Guardian with one component removed, with McNemar tests
    results/table_inloop.csv      in-loop transmitter faults named, by instrument type and fault
    results/table_process_blamed.csv  detected process faults Guardian blamed on a transmitter

Definitions (test split, diagnosis one hour after detection):
    detected        share of the 264 events with an alarm that starts at or after the event
    correct class   share of detected events whose class is right
    named           share of detected transmitter faults where the faulty tag is named
    false alarms    alarm onsets per day of normal operation
Intervals: Wilson score for proportions, exact Poisson for false-alarm rates,
bootstrap (2,000 resamples, fixed seed) for median delays.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardian.stats import boot_median, poisson_rate, wilson  # noqa: E402
from guardian.tags import TAGS  # noqa: E402

RES = ROOT / "results"
MAIN = ["Limit checks", "PCA + contributions", "PCA + RBC", "Guardian"]


def _named(d: pd.DataFrame) -> pd.Series:
    return (d.diag_class_1h == "sensor_fault") & (d.diag_tag_1h == d.true_tag)


def _row(label: str, est: tuple, k: int | None = None, n: int | None = None, **extra) -> dict:
    out = {"group": label, "value": est[0], "ci_low": est[1], "ci_high": est[2]}
    if k is not None:
        out.update(k=k, n=n)
    out.update(extra)
    return out


def main_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for m in MAIN:
        d = df[df.method == m]
        ev = d[d.true_class != "normal"]
        det = ev[ev.detected]
        sens = det[det.true_class == "sensor_fault"]
        fa = int(d.false_alarms.sum())
        days = d.normal_hours.sum() / 24
        warned = int((ev.detected & (ev.trip_t > ev.delay_h + ev.event_t)).sum())
        trips = int(ev.trip_t.notna().sum())
        for metric, est, k, n in [
            ("false_alarms_per_day", poisson_rate(fa, days), fa, round(days, 1)),
            ("detected", wilson(int(len(det)), len(ev)), len(det), len(ev)),
            ("median_delay_h", boot_median(det.delay_h.to_numpy()), None, len(det)),
            ("correct_class", wilson(int((det.diag_class_1h == det.true_class).sum()), len(det)),
             int((det.diag_class_1h == det.true_class).sum()), len(det)),
            ("transmitter_named", wilson(int(_named(sens).sum()), len(sens)), int(_named(sens).sum()), len(sens)),
            ("trips_warned", wilson(warned, trips), warned, trips),
        ]:
            rows.append({"method": m, "metric": metric, "value": est[0], "ci_low": est[1],
                         "ci_high": est[2], "k": k, "n": n})
    return pd.DataFrame(rows)


def loops_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for m in ["PCA + RBC", "Guardian"]:
        d = df[(df.method == m) & (df.true_class == "sensor_fault")].copy()
        d["in_loop"] = d.true_tag.map(lambda t: TAGS[t].in_control_loop)
        for loop, g in d.groupby("in_loop"):
            det = g[g.detected]
            rows.append({"method": m, "transmitters": "inside a control loop" if loop else "outside control loops",
                         "runs": len(g),
                         "detected": det.shape[0] / len(g),
                         "named_of_detected": _named(det).mean() if len(det) else np.nan,
                         "detected_k": len(det), "named_k": int(_named(det).sum())})
    return pd.DataFrame(rows)


def fault_type_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for m in ["PCA + RBC", "Guardian"]:
        d = df[(df.method == m) & (df.true_class == "sensor_fault") & df.detected]
        for kind, g in d.groupby("fault_kind"):
            p, lo, hi = wilson(int(_named(g).sum()), len(g))
            rows.append({"method": m, "fault_kind": kind, "detected_runs": len(g),
                         "named_of_detected": p, "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(rows)


def paired_table(df: pd.DataFrame) -> pd.DataFrame:
    """Exact McNemar test on the same 264 events: does Guardian detect more?"""
    g = df[(df.method == "Guardian") & (df.true_class != "normal")].set_index("scenario").detected
    rows = []
    for m in MAIN[:-1]:
        b = df[(df.method == m) & (df.true_class != "normal")].set_index("scenario").detected.reindex(g.index)
        only_g, only_b = int((g & ~b).sum()), int((~g & b).sum())
        p = stats.binomtest(only_b, only_g + only_b, 0.5).pvalue if only_g + only_b else 1.0
        rows.append({"baseline": m, "events": len(g), "only_guardian_detects": only_g,
                     "only_baseline_detects": only_b, "both": int((g & b).sum()),
                     "neither": int((~g & ~b).sum()), "mcnemar_exact_p": p})
    return pd.DataFrame(rows)


def attacks_table(att: pd.DataFrame) -> pd.DataFrame:
    """Per method, over the 42 attack runs (14 attack types x 3 seeds)."""
    rows = []
    for m in att.method.unique():
        a = att[att.method == m]
        k = int(round((a.detected * a.runs).sum()))
        n = int(a.runs.sum())
        p, lo, hi = wilson(k, n)
        rec = int(round((a.recognised_as_attack * a.runs).sum()))
        rows.append({"method": m, "attack_types": len(a), "runs": n, "detected_k": k, "detected": p,
                     "ci_low": lo, "ci_high": hi,
                     "median_of_type_median_delay_min": float(np.nanmedian(a.median_delay_min)),
                     "recognised_as_attack_k": rec, "trips": int(a.trips.sum()),
                     "trips_warned": int(a.trips_warned.sum())})
    return pd.DataFrame(rows)


def ablation_table(df: pd.DataFrame) -> pd.DataFrame:
    """Each variant against full Guardian on the same events (exact McNemar)."""
    ev = df[df.true_class != "normal"]
    full = ev[ev.method == "Guardian"].set_index("scenario")
    rows = []
    for m in [x for x in ev.method.unique() if x.startswith("Guardian")]:
        d = ev[ev.method == m].set_index("scenario").reindex(full.index)
        det = d[d.detected]
        sens = det[det.true_class == "sensor_fault"]
        row = {"variant": m}
        for name, ok_full, ok_var in [
            ("detected", full.detected, d.detected),
            ("correct_class", full.detected & (full.diag_class_1h == full.true_class),
             d.detected & (d.diag_class_1h == d.true_class)),
        ]:
            a, b = int((ok_full & ~ok_var).sum()), int((~ok_full & ok_var).sum())
            row[f"{name}_p_vs_full"] = stats.binomtest(min(a, b), a + b, 0.5).pvalue if a + b else 1.0
        row.update({
            "detected": d.detected.mean(),
            "correct_class_of_detected": (det.diag_class_1h == det.true_class).mean(),
            "transmitter_named_of_detected": _named(sens).mean(),
            "regime_correct_of_detected": (det[det.true_class == "regime_change"].diag_class_1h
                                           == "regime_change").mean(),
        })
        rows.append(row)
    return pd.DataFrame(rows)


def inloop_table(df: pd.DataFrame) -> pd.DataFrame:
    """Guardian, detected in-loop transmitter faults: named / detected, by ISA letter and fault kind."""
    from dashboard.plant import ISA

    d = df[(df.method == "Guardian") & (df.true_class == "sensor_fault") & df.detected].copy()
    d = d[d.true_tag.map(lambda t: TAGS[t].in_control_loop)]
    kind = {"F": "flow", "L": "level", "P": "pressure", "T": "temperature"}
    d["instrument"] = d.true_tag.map(lambda t: kind[ISA[t][0]])
    d["named"] = _named(d)
    out = d.groupby(["instrument", "fault_kind"]).named.agg(named="sum", detected="count").reset_index()
    out["named"] = out.named.astype(int)
    return out


def process_blamed_table(df: pd.DataFrame) -> pd.DataFrame:
    d = df[(df.method == "Guardian") & (df.true_class == "process_fault") & df.detected]
    b = d[d.diag_class_1h == "sensor_fault"]
    out = b.groupby(["idv", "diag_tag_1h"]).size().rename("runs").reset_index()
    out["detected_process_faults"] = len(d)
    return out


def misses(df: pd.DataFrame) -> pd.DataFrame:
    d = df[(df.method == "Guardian") & (df.true_class != "normal") & ~df.detected]
    return d[["scenario", "true_class", "true_tag", "fault_kind", "idv"]].reset_index(drop=True)


def main() -> None:
    df = pd.read_csv(RES / "benchmark_runs.csv")
    out = {
        "table_main.csv": main_table(df),
        "table_loops.csv": loops_table(df),
        "table_fault_type.csv": fault_type_table(df),
        "table_paired.csv": paired_table(df),
        "guardian_misses.csv": misses(df),
        "table_attacks.csv": attacks_table(pd.read_csv(RES / "attack_summary.csv")),
        "table_ablation.csv": ablation_table(df),
        "table_inloop.csv": inloop_table(df),
        "table_process_blamed.csv": process_blamed_table(df),
    }
    for name, t in out.items():
        t.to_csv(RES / name, index=False, float_format="%.6g")
    pd.set_option("display.width", 160)
    for name, t in out.items():
        print(f"\n== {name}\n{t.to_string(index=False)}")


if __name__ == "__main__":
    main()

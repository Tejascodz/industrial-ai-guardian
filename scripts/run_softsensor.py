"""Does Guardian keep the product-quality soft sensor trustworthy?

    python scripts/run_softsensor.py

For every test run the soft sensor runs as it would online. Guardian assigns a
status to each sample:
  OK             no alarm
  RECONSTRUCTED  sensor fault on a tag outside any control loop: the soft
                 sensor uses the virtual sensor instead of the transmitter
  UNTRUSTED      process fault, operation outside the validated envelope, or
                 a fault on a control-loop transmitter (the controller has
                 moved the real process, so the model is extrapolating; the
                 virtual sensor is still substituted for the bad input)
Errors are measured against the product analyzer, aligned for its dead time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guardian.evaluate import calibrate, persisted, prepare  # noqa: E402
from guardian.features import load_split, runs  # noqa: E402
from guardian.methods import FIELD_IDX, Guardian  # noqa: E402
from guardian.sim import NORMAL, SENSOR  # noqa: E402
from guardian.softsensor import SoftSensor, aligned_truth  # noqa: E402
from guardian.tags import MONITORED, TAGS  # noqa: E402

RESULTS = ROOT / "results"


def online_status(g: Guardian, p, limits) -> tuple[np.ndarray, np.ndarray]:
    """Per-sample status and reconstructed tag index (-1 if none)."""
    alarm = persisted((p.a.S > limits).any(axis=1))
    status = np.array(["OK"] * len(alarm), dtype=object)
    recon = np.full(len(alarm), -1)
    for i in np.flatnonzero(alarm):
        cls, tag = g.diagnose(p.a, i, limits)
        if cls == SENSOR:
            recon[i] = MONITORED.index(tag)
            status[i] = "UNTRUSTED" if TAGS[tag].in_control_loop else "RECONSTRUCTED"
        else:
            status[i] = "UNTRUSTED"
    return status, recon


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    train, _ = load_split("train")
    val, val_meta = load_split("val")
    test, test_meta = load_split("test")
    train_runs = [g for _, g in runs(train)]
    val_runs = list(runs(val))
    val_normal = [g for n, g in val_runs if val_meta[n]["event_class"] == NORMAL]

    g = Guardian().fit(train_runs, val_normal)
    limits, _ = calibrate(prepare(g, val_runs, val_meta), 0.25)
    ss = SoftSensor().fit(train_runs, scaler=g.scaler)

    rows = []
    for name, run in runs(test):
        m = test_meta[name]
        p = prepare(g, [(name, run)], test_meta)[0]
        status, recon = online_status(g, p, limits)
        x = g.scaler.transform(run)
        raw = ss.predict_x(x)
        protected = raw.copy()
        for k in set(recon[recon >= 0]):
            xk, _ = g.bank.reconstruct(x, k)
            alt = ss.predict_x(xk)
            protected[recon == k] = alt[recon == k]
        # Oracle: the soft sensor fed with the true (fault-free) transmitter values.
        xt = run.copy()
        for t in MONITORED:
            if t.startswith("xmeas_"):
                xt[t] = run[t.replace("xmeas_", "true_")]
        oracle = ss.predict_x(g.scaler.transform(xt))
        truth = aligned_truth(run)
        ev = m["event_t"] if m["event_class"] != NORMAL else np.inf
        rows.append(pd.DataFrame({
            "scenario": name, "true_class": m["event_class"],
            "fault_tag": (m["sensor_fault"] or {}).get("tag"),
            "time": run["time"], "after_event": run["time"].to_numpy() >= ev,
            "status": status, "truth": truth, "raw": raw, "protected": protected, "oracle": oracle,
        }))
    df = pd.concat(rows, ignore_index=True)
    df = df[df.truth.notna() & (df.time >= 4.0)]
    df.to_parquet(RESULTS / "softsensor_samples.parquet")

    def rmse(e):
        return float(np.sqrt(np.mean(np.square(e)))) if len(e) else np.nan

    out = []
    normal = df[~df.after_event]
    out.append(("Normal operation (all runs, before any event)", len(normal),
                rmse(normal.raw - normal.truth), np.nan))
    sf = df[(df.true_class == "sensor_fault") & df.after_event]
    out.append(("Sensor faults, after onset: raw soft sensor", len(sf), rmse(sf.raw - sf.truth), np.nan))
    out.append(("Sensor faults, after onset: Guardian-protected", len(sf), rmse(sf.protected - sf.truth), np.nan))
    out.append(("Sensor faults, after onset: oracle (true transmitter values)", len(sf),
                rmse(sf.oracle - sf.truth), np.nan))
    for st in ("OK", "RECONSTRUCTED", "UNTRUSTED"):
        d = df[df.status == st]
        out.append((f"All runs, samples with status {st}", len(d), rmse(d.raw - d.truth),
                    rmse(d.protected - d.truth)))
    for loop, d in sf.groupby(sf.fault_tag.map(lambda t: TAGS[t].in_control_loop)):
        lbl = "inside a control loop" if loop else "outside control loops"
        out.append((f"Sensor faults on transmitters {lbl}: raw / protected", len(d),
                    rmse(d.raw - d.truth), rmse(d.protected - d.truth)))
        out.append((f"Sensor faults on transmitters {lbl}: oracle", len(d), rmse(d.oracle - d.truth), np.nan))
    res = pd.DataFrame(out, columns=["subset", "samples", "rmse_raw", "rmse_protected"])
    # Are bad soft-sensor estimates caught? "Bad" = error above k x normal RMSE.
    base = rmse(normal.raw - normal.truth)
    err = (df.raw - df.truth).abs()
    flags = []
    for k in (3, 5, 10):
        bad = err > k * base
        flags.append({"error_above_x_normal_rmse": k, "bad_estimates": int(bad.sum()),
                      "share_flagged": float((bad & (df.status != "OK")).sum() / max(bad.sum(), 1)),
                      "bad_share_when_ok": float((bad & (df.status == "OK")).sum() / max((df.status == "OK").sum(), 1))})
    pd.DataFrame(flags).to_csv(RESULTS / "softsensor_flags.csv", index=False)
    res.to_csv(RESULTS / "softsensor_summary.csv", index=False)

    # Per faulty tag: does protection help where it matters?
    per_tag = (sf.assign(e_raw=(sf.raw - sf.truth) ** 2, e_prot=(sf.protected - sf.truth) ** 2)
                 .groupby("fault_tag")[["e_raw", "e_prot"]].mean() ** 0.5)
    per_tag.columns = ["rmse_raw", "rmse_protected"]
    per_tag.to_csv(RESULTS / "softsensor_by_tag.csv")
    with pd.option_context("display.width", 200):
        print(res.round(4).to_string(index=False))
        print(pd.DataFrame(flags).round(4).to_string(index=False))
        print(per_tag.round(4).sort_values("rmse_raw", ascending=False).head(10).to_string())


if __name__ == "__main__":
    main()

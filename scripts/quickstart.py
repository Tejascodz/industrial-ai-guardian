"""Smallest end-to-end example: inject one transmitter fault, read Guardian's verdict.

    python scripts/quickstart.py [--tag xmeas_18] [--kind bias] [--size 1.0]

Fits Guardian on the normal history in data/ (python scripts/generate_data.py
--only train val), calibrates its alarm limits to 0.25 false alarms per day,
then simulates a fresh 24-hour plant run with a fault injected on one
transmitter at hour 12 and prints what Guardian reports.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.plant import ISA  # noqa: E402
from guardian.evaluate import DIAG_LATER, calibrate, persisted, prepare  # noqa: E402
from guardian.features import BURN_IN_H, DATA, add_setpoints, load_split, runs, signature_runs  # noqa: E402
from guardian.methods import Guardian  # noqa: E402
from guardian.sim import BINARY, NORMAL, SENSOR, Scenario, SensorFault, run  # noqa: E402
from guardian.tags import FIELD_TAGS, TAGS  # noqa: E402

EVENT_T = 12.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", default="xmeas_18", choices=FIELD_TAGS, help="transmitter to fault")
    ap.add_argument("--kind", default="bias", choices=["bias", "drift", "stuck", "noise"])
    ap.add_argument("--size", type=float, default=1.0, help="fault size in engineering units")
    ap.add_argument("--seed", type=int, default=4242)
    args = ap.parse_args()

    if not BINARY.exists():
        sys.exit("simulator not built: run `make -C sim` first")
    if not (DATA / "val.parquet").exists():
        sys.exit("no data yet: run `python scripts/generate_data.py --only train val` first")

    t0 = time.time()
    train, _ = load_split("train")
    val, vm = load_split("val")
    vr = list(runs(val))
    g = Guardian().fit([r for _, r in runs(train)], [r for n, r in vr if vm[n]["event_class"] == NORMAL])
    g.fit_signatures(signature_runs())
    limits, _ = calibrate(prepare(g, vr, vm), 0.25)

    fault = SensorFault(args.tag, args.kind, EVENT_T, args.size)
    s = Scenario("quickstart", args.seed, hours=24.0, event_class=SENSOR, event_t=EVENT_T, sensor_fault=fault)
    df = add_setpoints(run(s), {s.name: asdict(s)})
    df = df[df["time"] >= BURN_IN_H].reset_index(drop=True)

    a = g.analyze(df)
    alarm = persisted((a.S > limits).any(axis=1))
    t = df["time"].to_numpy()
    rising = alarm & ~np.r_[False, alarm[:-1]]
    early = int(np.sum(rising & (t < EVENT_T)))
    after = np.flatnonzero(rising & (t >= EVENT_T))

    tag = TAGS[args.tag]
    what = args.kind if args.kind == "stuck" else f"{args.kind} of {args.size:g} {tag.unit}"
    print(f"injected  {what} on {ISA[args.tag]} ({tag.description}) at t = {EVENT_T:g} h")
    print(f"before    {early} false alarm(s) in {EVENT_T - BURN_IN_H:g} h of normal operation")
    if not len(after):
        print("alarm     none")
    else:
        i = int(after[0])
        j = min(i + DIAG_LATER, len(t) - 1)
        print(f"alarm     t = {t[i]:.2f} h, {60 * (t[i] - EVENT_T):.0f} min after the fault")
        for label, k in (("verdict", i), ("1 h later", j)):
            cls, suspect = g.diagnose(a, k, limits)
            who = f" on {ISA[suspect]} ({TAGS[suspect].description})" if suspect else ""
            print(f"{label:9s} {cls.replace('_', ' ')}{who}")
    print(f"took {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
